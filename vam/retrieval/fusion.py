from copy import deepcopy
import math


def fuse(rankings, *, strategy='rrf', k=60):
    """Each key is an independent source/query list. Repeated IDs count once per list."""
    if strategy not in {'rrf', 'legacy'} or not math.isfinite(k) or k < 1:
        raise ValueError('Invalid fusion strategy or RRF k')
    merged = {}
    for channel in sorted(rankings):
        seen = set()
        rank = 0
        for item in rankings[channel]:
            eid = item['evidence_id']
            if eid in seen:
                continue
            seen.add(eid)
            rank += 1
            raw = item['score_details']['similarity']
            similarity = float(raw) if raw is not None else None
            if similarity is not None and not math.isfinite(similarity):
                raise ValueError('Invalid similarity')
            if eid not in merged:
                merged[eid] = deepcopy(item)
                merged[eid]['score'] = 0 if strategy == 'rrf' else (similarity or 0)
                merged[eid]['score_details'] = {'strategy': strategy, 'channels': {}, 'rrf_k': k}
            result = merged[eid]
            contribution = 1 / (k + rank)
            result['score_details']['channels'][channel] = {'rank': rank, 'similarity': similarity, 'rrf': contribution,
                                                           'selection': item['score_details'].get('selection', 'semantic')}
            result['score'] = result['score'] + contribution if strategy == 'rrf' else max(result['score'], similarity or 0)
    return sorted(merged.values(), key=lambda e: (-e['score'], e['start_t'], e['evidence_id']))
