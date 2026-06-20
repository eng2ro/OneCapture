"""Idempotency / emission store.

Pass 1 ships a single :class:`Store` with two backends selected by whether a
file path is given:

* **in-memory** (``path=None``) — for tests and one-shot runs.
* **JSON file** — so committed line keys survive across process runs and a
  re-import of the same file commits zero new records.

The public surface is deliberately narrow (``known_keys``, ``commit``,
``entries``) so a Postgres / multi-tenant backend can drop in behind the same
interface later. Multi-tenancy is modelled today by keying everything on the
``client_id`` already embedded in each line key.

Decimals round-trip losslessly because emission entries are dumped with
pydantic's JSON mode (Decimal -> string) and re-parsed through the model.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.models import EmissionEntry

LineKey = tuple[str, str, int]


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path: Path | None = Path(path) if path is not None else None
        self._entries: list[EmissionEntry] = []
        self._keys: set[LineKey] = set()
        if self._path is not None and self._path.exists():
            self._load()

    # -- queries ----------------------------------------------------------- #
    def known_keys(self, client_id: str | None = None) -> set[LineKey]:
        if client_id is None:
            return set(self._keys)
        return {k for k in self._keys if k[0] == client_id}

    def entries(self, client_id: str | None = None) -> list[EmissionEntry]:
        if client_id is None:
            return list(self._entries)
        return [e for e in self._entries if e.line_key[0] == client_id]

    def __len__(self) -> int:
        return len(self._entries)

    # -- commit ------------------------------------------------------------ #
    def commit(self, entries: list[EmissionEntry]) -> int:
        """Append entries whose line key is new; persist. Returns count added.

        Idempotent: entries whose key is already known are silently skipped, so
        re-importing a committed file adds nothing.
        """
        added = 0
        for entry in entries:
            key = _as_key(entry.line_key)
            if key in self._keys:
                continue
            self._keys.add(key)
            self._entries.append(entry)
            added += 1
        if added and self._path is not None:
            self._persist()
        return added

    # -- backend ----------------------------------------------------------- #
    def _persist(self) -> None:
        assert self._path is not None
        payload = {
            "version": 1,
            "entries": [json.loads(e.model_dump_json()) for e in self._entries],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._path)  # atomic-ish swap

    def _load(self) -> None:
        assert self._path is not None
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for raw in data.get("entries", []):
            entry = EmissionEntry.model_validate(raw)
            self._entries.append(entry)
            self._keys.add(_as_key(entry.line_key))


def _as_key(line_key) -> LineKey:
    # JSON round-trips tuples as lists; normalise back to a hashable tuple.
    client, doc, line = line_key
    return (str(client), str(doc), int(line))
