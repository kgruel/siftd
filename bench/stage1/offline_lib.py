#!/usr/bin/env python3
"""Offline-fusion substrate for the stage-1 sweep + fidelity gate.

This is the load-bearing replica of the live search engine's ranking math. The
sweep fuses thousands of configs over cached per-query artifacts (no re-embedding,
no re-searching); the fidelity gate proves that this offline replica reproduces the
LIVE engine's top-10 EXACTLY at the two configs that exist as engine knobs (default
narrow-then-rank, and SIFTD_HYBRID_STRATEGY=rrf). Every function here is a faithful
port of the corresponding engine path so the gate is a real independent check, not a
tautology of calling the same code.

Engine sources mirrored (do not drift from these):
  * siftd.storage.embeddings._EmbeddingCache.load  -> ArmData.load
  * siftd.storage.embeddings.search_similar        -> replica_search_similar
  * siftd.search.mmr_rerank                        -> replica_mmr_rerank
  * siftd.api.search._build_vector_list            -> _vector_list
  * siftd.api.search._narrow_then_rank             -> replica_narrow
  * siftd.api.search hybrid rrf branch + _fuse_hybrid -> replica_rrf / _fuse
  * siftd.storage.embeddings.chunks_for_events     -> ArmData.chunks_for_events
  * siftd.storage.fts (search_content / fts5_recall_conversations) -> cached upstream

Determinism note (narrow-then-rank): the engine builds the vector candidate index
by iterating a Python *set* of conversation ids (search_similar's conversation_ids
filter). Set iteration order of strings is PYTHONHASHSEED-dependent, so the tie
order at the top-k cosine boundary is too — a latent, benign engine property that
only bites on exact float32 cosine ties. The gate pins PYTHONHASHSEED=0 and this
replica reconstructs the narrowed set from the cached recall list in the same order,
so engine and replica agree bit-for-bit. RRF has no such dependence (its vector list
runs over the full matrix in natural row order).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Engine constants (kept in lockstep with the source — asserted against import in the
# gate so a drift in the engine is caught, not silently diverged from).
RRF_K = 60  # siftd.api.search._RRF_K
MMR_CAP = 1000  # siftd.search.MAX_MMR_CANDIDATES — engine truncates before MMR
DEFAULT_LAMBDA = 0.7  # search default rerank lambda
DEFAULT_RECALL = 80  # hybrid_search default recall
DEFAULT_N = 10  # top-n the gate compares


# --------------------------------------------------------------------------- #
# Arm data: the whole chunk matrix + the lookups, loaded once.
# --------------------------------------------------------------------------- #


@dataclass
class ArmData:
    """The in-memory image of one arm's embed DB — the offline analogue of the
    engine's process-global ``_EmbeddingCache``. Loaded once (203k x 1024 f32 ~=
    830MB) and shared by the sweep and the gate."""

    matrix: np.ndarray  # (n, dim) float32, L2-normalized — same as the engine's cache
    chunk_ids: list[str]
    conversation_ids: list[str]
    source_ids_raw: list[str | None]
    conv_id_to_indices: dict[str, list[int]] = field(default_factory=dict)
    chunk_id_to_index: dict[str, int] = field(default_factory=dict)
    _event_map: dict[str, list[int]] | None = None

    @classmethod
    def load(cls, embed_db: Path) -> ArmData:
        """Load + normalize identically to _EmbeddingCache.load (same SELECT, same
        natural row order, same L2 normalization) so global row indices align with
        what the engine computes."""
        conn = sqlite3.connect(f"file:{embed_db.as_posix()}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, conversation_id, chunk_type, text, embedding, source_ids FROM chunks"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            raise SystemExit(f"empty embed DB: {embed_db}")

        dim = len(rows[0]["embedding"]) // 4
        blob = b"".join(r["embedding"] for r in rows)
        mat = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), dim).copy()
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        mat /= norms  # in-place, exactly as the engine

        chunk_ids = [r["id"] for r in rows]
        conversation_ids = [r["conversation_id"] for r in rows]
        source_ids_raw = [r["source_ids"] for r in rows]

        conv_id_to_indices: dict[str, list[int]] = {}
        for i, cid in enumerate(conversation_ids):
            conv_id_to_indices.setdefault(cid, []).append(i)
        chunk_id_to_index = {cid: i for i, cid in enumerate(chunk_ids)}

        return cls(
            matrix=mat,
            chunk_ids=chunk_ids,
            conversation_ids=conversation_ids,
            source_ids_raw=source_ids_raw,
            conv_id_to_indices=conv_id_to_indices,
            chunk_id_to_index=chunk_id_to_index,
        )

    @property
    def dim(self) -> int:
        return self.matrix.shape[1]

    def event_map(self) -> dict[str, list[int]]:
        """event_id -> covering chunk row indices, built once from source_ids in row
        order (mirrors _EmbeddingCache.ensure_event_map)."""
        if self._event_map is None:
            m: dict[str, list[int]] = {}
            for i, raw in enumerate(self.source_ids_raw):
                if not raw:
                    continue
                for eid in json.loads(raw):
                    m.setdefault(eid, []).append(i)
            self._event_map = m
        return self._event_map

    def chunks_for_events(self, event_ids: list[str]) -> dict[str, list[dict]]:
        """Mirror storage.embeddings.chunks_for_events: de-dup preserving order, map
        each event to its covering chunk(s)."""
        em = self.event_map()
        out: dict[str, list[dict]] = {}
        for eid in dict.fromkeys(event_ids):
            idxs = em.get(eid)
            if not idxs:
                continue
            out[eid] = [
                {
                    "chunk_id": self.chunk_ids[i],
                    "conversation_id": self.conversation_ids[i],
                    "source_ids": json.loads(self.source_ids_raw[i]) if self.source_ids_raw[i] else [],
                }
                for i in idxs
            ]
        return out


# --------------------------------------------------------------------------- #
# Vector stage: cosine top-k + MMR (faithful ports).
# --------------------------------------------------------------------------- #


def replica_search_similar(
    arm: ArmData,
    query_embedding: list[float],
    *,
    limit: int,
    conversation_ids: set[str] | None,
    include_embeddings: bool,
) -> list[dict]:
    """Port of storage.embeddings.search_similar (cosine, argpartition top-k).

    Reproduces the engine's numpy ops exactly, including the candidate-index
    construction order (set iteration) that governs tie-order at the top-k boundary.
    """
    if conversation_ids is not None and not conversation_ids:
        return []

    embedding_dim = arm.dim
    if len(query_embedding) != embedding_dim:
        raise ValueError(f"query dim {len(query_embedding)} != index dim {embedding_dim}")

    if conversation_ids is not None:
        indices: list[int] = []
        for cid in conversation_ids:  # set iteration — see determinism note
            indices.extend(arm.conv_id_to_indices.get(cid, []))
        if not indices:
            return []
        indices_arr: np.ndarray | None = np.array(indices, dtype=np.intp)
        candidate_norm = arm.matrix[indices_arr]
    else:
        indices_arr = None
        candidate_norm = arm.matrix

    query_array = np.asarray(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_array)
    if query_norm == 0:
        scores = np.zeros(candidate_norm.shape[0], dtype=np.float32)
    else:
        scores = candidate_norm @ (query_array / query_norm)

    n = len(scores)
    if n <= limit:
        top_local = np.argsort(-scores)
    else:
        k = min(limit, n)
        partitioned = np.argpartition(-scores, k)[:k]
        top_local = partitioned[np.argsort(-scores[partitioned])]

    results: list[dict] = []
    for local_idx in top_local:
        local_i = int(local_idx)
        score_val = float(scores[local_i])
        global_i = int(indices_arr[local_i]) if indices_arr is not None else local_i
        raw = arm.source_ids_raw[global_i]
        r = {
            "chunk_id": arm.chunk_ids[global_i],
            "conversation_id": arm.conversation_ids[global_i],
            "score": score_val,
            "source_ids": json.loads(raw) if raw else [],
        }
        if include_embeddings:
            r["embedding"] = arm.matrix[global_i]
        results.append(r)
    return results


def replica_mmr_rerank(
    results: list[dict],
    query_embedding: list[float],
    *,
    lambda_: float,
    limit: int,
) -> list[dict]:
    """Port of siftd.search.mmr_rerank — conversation-level hard suppression + cosine
    diversity, with the (mmr_score, chunk_id) strict tie-break. Returns selected
    results in MMR order (without the embedding key)."""
    if not results:
        return []

    n = len(results)
    embeddings = np.vstack([
        r["embedding"] if isinstance(r["embedding"], np.ndarray) else np.asarray(r["embedding"], dtype=np.float32)
        for r in results
    ])
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    embeddings_normalized = embeddings / norms

    relevances = np.array([r["score"] for r in results], dtype=np.float32)
    conv_ids = [r["conversation_id"] for r in results]
    chunk_ids = [r.get("chunk_id", "") or "" for r in results]

    remaining = set(range(n))
    selected: list[int] = []
    selected_convs: set[str] = set()
    max_sim_to_selected = np.zeros(n, dtype=np.float32)

    while remaining and len(selected) < limit:
        best_idx = -1
        best_score = float("-inf")
        best_chunk_id = ""
        for idx in remaining:
            conv_id = conv_ids[idx]
            if conv_id in selected_convs:
                penalty = 1.0
            elif selected:
                penalty = float(max_sim_to_selected[idx])
            else:
                penalty = 0.0
            mmr_score = lambda_ * relevances[idx] - (1 - lambda_) * penalty
            chunk_id = chunk_ids[idx]
            if (mmr_score, chunk_id) > (best_score, best_chunk_id):
                best_score = mmr_score
                best_idx = idx
                best_chunk_id = chunk_id
        remaining.remove(best_idx)
        selected.append(best_idx)
        selected_convs.add(conv_ids[best_idx])
        if remaining:
            new_vec = embeddings_normalized[best_idx]
            for idx in remaining:
                sim = float(np.dot(embeddings_normalized[idx], new_vec))
                if sim > max_sim_to_selected[idx]:
                    max_sim_to_selected[idx] = sim

    out: list[dict] = []
    for idx in selected:
        r = dict(results[idx])
        r.pop("embedding", None)
        out.append(r)
    return out


def _vector_list(
    arm: ArmData,
    query_embedding: list[float],
    candidate_ids: set[str] | None,
    *,
    search_limit: int,
    mmr_limit: int,
    lambda_: float,
) -> list[dict]:
    """Port of _build_vector_list for the use_mmr=True, recency=False path (the gate's
    config): cosine top-`search_limit`, MMR-cap truncation, MMR to `mmr_limit`, stamp
    1-based vector_rank. The cap never binds at the gate configs (pool=30) but keeps
    the replica faithful when the sweep pushes pool depths toward MMR_CAP."""
    results = replica_search_similar(
        arm, query_embedding, limit=search_limit, conversation_ids=candidate_ids, include_embeddings=True
    )
    if not results:
        return []
    if len(results) > MMR_CAP:
        results = sorted(results, key=lambda r: -r["score"])[:MMR_CAP]
    results = replica_mmr_rerank(results, query_embedding, lambda_=lambda_, limit=mmr_limit)
    for i, r in enumerate(results):
        r["vector_rank"] = i + 1
    return results


# --------------------------------------------------------------------------- #
# The two engine configs, replicated from cached artifacts.
# --------------------------------------------------------------------------- #


def replica_narrow(
    arm: ArmData,
    query_embedding: list[float],
    recall_ids: list[str],
    *,
    n: int = DEFAULT_N,
    recall: int = DEFAULT_RECALL,
    lambda_: float = DEFAULT_LAMBDA,
) -> list[dict]:
    """Port of _narrow_then_rank (default hybrid): FTS recall narrows candidates,
    embeddings rerank. ``recall_ids`` is the cached ordered recall list (limit >=
    recall); the engine's limit=`recall` set is its prefix. candidate_ids is None in
    the closed-universe gate config (include_derivative=True, exclude_active=False)."""
    narrowed: set[str] | None = None
    if recall_ids:
        narrowed = set(recall_ids[:recall])
    if narrowed is not None and not narrowed:
        return []
    # use_mmr=True: search_limit = max(n*3, n), mmr_limit = n
    return _vector_list(
        arm, query_embedding, narrowed,
        search_limit=max(n * 3, n), mmr_limit=n, lambda_=lambda_,
    )


@dataclass
class _FusionItem:
    result: dict
    vector_rank: int | None
    keyword_rank: int | None

    def note_keyword_rank(self, rank: int) -> None:
        if self.keyword_rank is None or rank < self.keyword_rank:
            self.keyword_rank = rank

    def fused_score(self) -> float:
        fused = 0.0
        if self.vector_rank is not None:
            fused += 1.0 / (RRF_K + self.vector_rank)
        if self.keyword_rank is not None:
            fused += 1.0 / (RRF_K + self.keyword_rank)
        return fused


def _fuse(vector_results: list[dict], keyword_hits: list[dict], arm: ArmData, *, n: int) -> list[dict]:
    """Port of _fuse_hybrid: RRF over vector_rank + keyword_rank, FTS->chunk bridge,
    entrant handling, sort by (-fused, key)."""
    fusion: dict[str, _FusionItem] = {}
    for r in vector_results:
        fusion[r["chunk_id"]] = _FusionItem(result=r, vector_rank=r.get("vector_rank"), keyword_rank=None)

    event_ids = [h["event_id"] for h in keyword_hits if h.get("event_id")]
    covering = arm.chunks_for_events(event_ids)

    for pos, hit in enumerate(keyword_hits):
        kr = pos + 1
        eid = hit.get("event_id")
        chunks = covering.get(eid) if eid else None
        if chunks:
            for ch in chunks:
                cid = ch["chunk_id"]
                item = fusion.get(cid)
                if item is None:
                    item = _FusionItem(
                        result={"chunk_id": ch["chunk_id"], "conversation_id": ch["conversation_id"],
                                "source_ids": ch.get("source_ids") or []},
                        vector_rank=None, keyword_rank=None,
                    )
                    fusion[cid] = item
                item.note_keyword_rank(kr)
        elif eid:
            ekey = f"entrant:{eid}"
            item = fusion.get(ekey)
            if item is None:
                fusion[ekey] = _FusionItem(
                    result={"chunk_id": None, "conversation_id": hit["conversation_id"],
                            "event_id": eid, "source_ids": []},
                    vector_rank=None, keyword_rank=kr,
                )
            else:
                item.note_keyword_rank(kr)

    scored = sorted(fusion.items(), key=lambda kv: (-kv[1].fused_score(), kv[0]))
    out: list[dict] = []
    for _key, item in scored[:n]:
        r = dict(item.result)
        r["score"] = item.fused_score()
        r["vector_rank"] = item.vector_rank
        r["keyword_rank"] = item.keyword_rank
        out.append(r)
    return out


def replica_rrf(
    arm: ArmData,
    query_embedding: list[float],
    keyword_hits: list[dict],
    *,
    n: int = DEFAULT_N,
    lambda_: float = DEFAULT_LAMBDA,
) -> list[dict]:
    """Port of the hybrid rrf branch: full-set vector list (no FTS narrowing) fused
    with the chunk-level keyword list. ``keyword_hits`` is the cached fts_content list
    (event-level, ordered by bm25 rank); the engine uses the top ``max(pool, n)``."""
    pool = max(n * 3, n)
    vector_results = _vector_list(
        arm, query_embedding, None,
        search_limit=pool, mmr_limit=pool, lambda_=lambda_,
    )
    kw = keyword_hits[: max(pool, n)]
    return _fuse(vector_results, kw, arm, n=n)


# --------------------------------------------------------------------------- #
# Query set loading (shared by cache + gate).
# --------------------------------------------------------------------------- #

GT_FILES = ["gt-identifier", "gt-tool", "gt-topical", "gt-paraphrase", "gt-mixed"]


def load_queries(run_dir: Path) -> list[dict]:
    """Load all ground-truth queries in a deterministic order (file order above, then
    line order). Each row gets a stable qid ``<class>-<lineno:04d>``."""
    out: list[dict] = []
    for stem in GT_FILES:
        path = run_dir / f"{stem}.jsonl"
        cls = stem[len("gt-"):]
        for i, line in enumerate(path.read_text().splitlines()):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append({
                "qid": f"{cls}-{i:04d}",
                "class": d.get("class", cls),
                "query": d["query"],
                "labels": d.get("labels", []),
                "meta": d.get("meta", {}),
            })
    return out
