"""Перенос коллекций «Кванта» из старого Qdrant (Amvera) в новый — через API.

Снимок (snapshot) Qdrant переносится только между совместимыми версиями, а
перенос точек через API от версии не зависит: читаем точки с векторами и
payload, создаём коллекцию с теми же настройками и индексами, записываем.

Запуск (из deploy/timeweb, когда стек поднят):
    docker compose run --rm -v "$PWD/migrate_qdrant.py:/m.py:ro" bot \\
        python /m.py https://qdrantdb-xxx.amvera.io СТАРЫЙ_КЛЮЧ
"""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    old_url, old_key = sys.argv[1].rstrip("/"), sys.argv[2]
    new_url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    new_key = os.getenv("QDRANT_API_KEY", "")
    old = httpx.Client(base_url=old_url, headers={"api-key": old_key}, timeout=120)
    new = httpx.Client(base_url=new_url, headers={"api-key": new_key}, timeout=120)

    names = [c["name"] for c in old.get("/collections").json()["result"]["collections"]]
    print(f"Коллекции в старом Qdrant: {names}")
    for name in names:
        info = old.get(f"/collections/{name}").json()["result"]
        params = info["config"]["params"]
        body = {"vectors": params["vectors"]}
        if params.get("sparse_vectors"):
            body["sparse_vectors"] = params["sparse_vectors"]
        if new.get(f"/collections/{name}").status_code == 200:
            print(f"[{name}] уже есть в новом Qdrant — дописываю точки поверх")
        else:
            new.put(f"/collections/{name}", json=body).raise_for_status()
            print(f"[{name}] создана: {params['vectors']}")
        for field, schema in (info.get("payload_schema") or {}).items():
            new.put(f"/collections/{name}/index", json={"field_name": field, "field_schema": schema["data_type"]})

        offset, total = None, 0
        while True:
            request = {"limit": 128, "with_payload": True, "with_vector": True}
            if offset is not None:
                request["offset"] = offset
            page = old.post(f"/collections/{name}/points/scroll", json=request).json()["result"]
            points = [
                {"id": p["id"], "vector": p.get("vector"), "payload": p.get("payload") or {}}
                for p in page.get("points", [])
            ]
            if points:
                new.put(f"/collections/{name}/points?wait=true", json={"points": points}).raise_for_status()
                total += len(points)
            offset = page.get("next_page_offset")
            if offset is None:
                break
        count = new.post(f"/collections/{name}/points/count", json={"exact": True}).json()["result"]["count"]
        print(f"[{name}] перенесено точек: {total}, в новом Qdrant: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
