"""Minimal glTF 2.0 binary (.glb) writer: indexed triangle meshes with normals,
vertex colours (linear RGB), planar UVs and a per-vertex `_FEATURE_ID` attribute
(index into features.json), optionally with one embedded JPEG texture."""
from __future__ import annotations

import json
import struct

import numpy as np

FLOAT, UINT32 = 5126, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963


def write_glb(path, meshes, extras=None, image_jpeg: bytes | None = None):
    """meshes: list of dicts with keys
         name, pos (N,3), nrm (N,3), col (N,3) linear, uv (N,2), fid (N,), idx (M,3),
         material: {name, roughness, double_sided, textured}
    """
    binbuf = bytearray()
    views, accessors = [], []

    def view(data: bytes, target=None):
        while len(binbuf) % 4:
            binbuf.append(0)
        v = {"buffer": 0, "byteOffset": len(binbuf), "byteLength": len(data)}
        if target:
            v["target"] = target
        binbuf.extend(data)
        views.append(v)
        return len(views) - 1

    def accessor(arr, ctype, typ, target, minmax=False):
        arr = np.ascontiguousarray(arr)
        a = {"bufferView": view(arr.tobytes(), target), "componentType": ctype,
             "count": int(arr.shape[0]), "type": typ}
        if minmax:
            a["min"] = [float(v) for v in arr.min(axis=0)]
            a["max"] = [float(v) for v in arr.max(axis=0)]
        accessors.append(a)
        return len(accessors) - 1

    gl_meshes, nodes, materials = [], [], []
    for m in meshes:
        attrs = {
            "POSITION": accessor(m["pos"].astype(np.float32), FLOAT, "VEC3", ARRAY_BUFFER, minmax=True),
            "NORMAL": accessor(m["nrm"].astype(np.float32), FLOAT, "VEC3", ARRAY_BUFFER),
        }
        if m.get("col") is not None:
            attrs["COLOR_0"] = accessor(m["col"].astype(np.float32), FLOAT, "VEC3", ARRAY_BUFFER)
        if m.get("uv") is not None:
            attrs["TEXCOORD_0"] = accessor(m["uv"].astype(np.float32), FLOAT, "VEC2", ARRAY_BUFFER)
        if m.get("fid") is not None:
            attrs["_FEATURE_ID"] = accessor(m["fid"].astype(np.float32), FLOAT, "SCALAR", ARRAY_BUFFER)
        for name, arr in (m.get("extra_attrs") or {}).items():   # custom attributes must start with "_"
            arr = np.asarray(arr, np.float32)
            typ = {1: "SCALAR", 2: "VEC2", 3: "VEC3", 4: "VEC4"}[1 if arr.ndim == 1 else arr.shape[1]]
            attrs[name] = accessor(arr, FLOAT, typ, ARRAY_BUFFER)
        idx = accessor(m["idx"].astype(np.uint32).reshape(-1), UINT32, "SCALAR", ELEMENT_ARRAY_BUFFER)
        mat = m.get("material", {})
        pbr = {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0.0,
               "roughnessFactor": float(mat.get("roughness", 0.9))}
        if mat.get("textured") and image_jpeg is not None:
            pbr["baseColorTexture"] = {"index": 0, "texCoord": 0}
        materials.append({"name": mat.get("name", m["name"]), "pbrMetallicRoughness": pbr,
                          "doubleSided": bool(mat.get("double_sided", False))})
        gl_meshes.append({"name": m["name"], "primitives": [
            {"attributes": attrs, "indices": idx, "material": len(materials) - 1, "mode": 4}]})
        nodes.append({"name": m["name"], "mesh": len(gl_meshes) - 1})

    gltf = {
        "asset": {"version": "2.0", "generator": "the-gap-vr pipeline"},
        "scene": 0,
        "scenes": [{"name": "The Gap - Yuruga Street", "nodes": list(range(len(nodes))),
                    "extras": extras or {}}],
        "nodes": nodes, "meshes": gl_meshes, "materials": materials,
        "accessors": accessors, "bufferViews": views,
    }
    if image_jpeg is not None:
        iv = view(image_jpeg)
        gltf["images"] = [{"bufferView": iv, "mimeType": "image/jpeg", "name": "aerial_ortho"}]
        gltf["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]
        gltf["textures"] = [{"sampler": 0, "source": 0}]
    while len(binbuf) % 4:
        binbuf.append(0)
    gltf["buffers"] = [{"byteLength": len(binbuf)}]
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(binbuf)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<I4s", len(js), b"JSON"))
        f.write(js)
        f.write(struct.pack("<I4s", len(binbuf), b"BIN\x00"))
        f.write(binbuf)
