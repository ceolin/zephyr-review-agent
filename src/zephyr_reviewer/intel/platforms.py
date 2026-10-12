# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Board and platform identifiers, derived from board.yml and the DTS files next to it.

A twister platform identifier is ``board/soc[/cpucluster][/variant]``. board.yml names the board
and its SoCs; the qualifiers come from the devicetree file names in the board directory
(``nrf5340dk_nrf5340_cpuapp_ns.dts`` under board ``nrf5340dk`` with SoC ``nrf5340`` is
``nrf5340dk/nrf5340/cpuapp/ns``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from .repo import GitRepo


@dataclass
class Board:
    directory: str  # boards/<vendor>/<board>
    name: str
    vendor: str = ""
    full_name: str = ""
    socs: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)


class PlatformIndex:
    def __init__(self, boards: list[Board]):
        self.boards = boards
        self.by_dir: dict[str, list[Board]] = {}
        self.by_identifier: dict[str, Board] = {}
        for b in boards:
            self.by_dir.setdefault(b.directory, []).append(b)
            for ident in b.identifiers:
                self.by_identifier[ident] = b

    @classmethod
    def build(cls, repo: GitRepo) -> PlatformIndex:
        files = repo.files()
        yml_paths = [p for p in files if p.startswith("boards/") and p.endswith("/board.yml")]
        contents = repo.show_many(yml_paths)
        dts_by_dir: dict[str, list[str]] = {}
        for p in files:
            if p.startswith("boards/") and p.endswith(".dts"):
                d, _, name = p.rpartition("/")
                dts_by_dir.setdefault(d, []).append(name[: -len(".dts")])
        boards: list[Board] = []
        for path, text in contents.items():
            directory = path.rsplit("/", 1)[0]
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                continue
            entries = []
            if isinstance(data.get("board"), dict):
                entries.append(data["board"])
            for b in data.get("boards") or []:
                if isinstance(b, dict):
                    entries.append(b)
            for entry in entries:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                socs = [str(s.get("name")) for s in entry.get("socs") or [] if isinstance(s, dict) and s.get("name")]
                board = Board(
                    directory=directory,
                    name=name,
                    vendor=str(entry.get("vendor") or ""),
                    full_name=str(entry.get("full_name") or ""),
                    socs=socs,
                )
                board.identifiers = _identifiers(name, socs, dts_by_dir.get(directory, []))
                boards.append(board)
        boards.sort(key=lambda b: b.name)
        return cls(boards)

    def for_path(self, path: str) -> list[str]:
        """Identifiers of the board(s) whose directory contains ``path`` (boards/<vendor>/<board>/...)."""
        parts = path.split("/")
        for depth in range(len(parts) - 1, 1, -1):
            d = "/".join(parts[:depth])
            if d in self.by_dir:
                return sorted(i for b in self.by_dir[d] for i in b.identifiers)
        return []

    def search(self, fragment: str, limit: int = 60) -> list[str]:
        frag = fragment.lower().strip()
        hits = [i for i in self.by_identifier if frag in i.lower()]
        if not hits:
            hits = [
                i for b in self.boards if frag in b.full_name.lower() or frag in b.vendor.lower() for i in b.identifiers
            ]
        return sorted(hits)[:limit]

    def exists(self, identifier: str) -> bool:
        return identifier in self.by_identifier


def _identifiers(board: str, socs: list[str], dts_stems: list[str]) -> list[str]:
    out: set[str] = set()
    prefix = board + "_"
    for stem in dts_stems:
        if not stem.startswith(prefix):
            continue
        rest = stem[len(prefix) :]
        for soc in sorted(socs, key=len, reverse=True):
            if rest == soc:
                out.add(f"{board}/{soc}")
                break
            if rest.startswith(soc + "_"):
                qualifiers = rest[len(soc) + 1 :].replace("_", "/")
                out.add(f"{board}/{soc}/{qualifiers}")
                break
    if not out:
        out.update(f"{board}/{soc}" for soc in socs)
    return sorted(out)
