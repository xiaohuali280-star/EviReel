"""E4 answer routing: a low-cost path and a deep path, chosen by explicit rules.

``deep``  -- the pre-E4 behaviour: visual inspection of the evidence frames (VLM) + grounded answer (LLM).
``cheap`` -- grounded answer over the retrieved evidence only, no VLM call; nothing strong -> refuse.
``auto``  -- assess the retrieved evidence first:
             sufficient                      -> cheap answer;
             not sufficient, something strong
             or the question is about visuals -> ONE escalation round: inspect at most RETRIEVAL_INSPECT_FRAMES
                                                frames within ROUTE_ESCALATION_TIMEOUT_S, then the deep answer;
             nothing strong, not visual       -> refuse without any model call (sources still returned).

"Strong" evidence (the rules are deliberately simple and their thresholds are evaluated, not assumed):
* a semantic channel scored it at least ROUTE_MIN_SIMILARITY, or
* the keyword channel found at least ROUTE_MIN_KEYWORD_COVERAGE of the question's distinct tokens in it.
"Sufficient" = at least one strong item, and for a visual question the frames have been inspected.
A cheap answer the model marks uncertain is escalated once (inspection + a second answer) when frames exist.

Every answer carries ``status``: ``answered``, ``uncertain`` (the model or the rules flagged insufficient
evidence) or ``refused`` (no answer given); and ``route`` records the decisions, so the evaluation can
attribute calls, latency and cost to each path.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VISUAL_TERMS = ('画面', '屏幕', '幻灯片', '板书', '图上', '图中', '图里', '显示', '写着', '写了', '写的', '公式', '代码',
                'ppt', 'slide', 'screen', 'shown', 'visible', 'diagram', 'whiteboard', 'formula', 'code on')


def needs_visual(question: str) -> bool:
    q = (question or '').casefold()
    return any(term in q for term in VISUAL_TERMS)


@dataclass
class Assessment:
    sufficient: bool
    strong: list = field(default_factory=list)
    needs_visual: bool = False
    reasons: list = field(default_factory=list)

    def as_dict(self):
        return {'sufficient': self.sufficient, 'strong_evidence_ids': self.strong, 'needs_visual': self.needs_visual,
                'reasons': self.reasons}


def signals(rankings) -> dict:
    """Best semantic similarity and best keyword coverage per evidence id, from the per-channel lists
    (before fusion, where RRF has replaced the raw scores)."""
    out = {}
    for key, items in rankings.items():
        for item in items:
            details = item.get('score_details') or {}
            entry = out.setdefault(item['evidence_id'], {'similarity': None, 'coverage': None, 'source': item['source']})
            if details.get('selection') == 'keyword':
                coverage = details.get('coverage')
                if coverage is not None:
                    entry['coverage'] = max(entry['coverage'] or 0, float(coverage))
            elif details.get('similarity') is not None and details.get('selection', 'semantic') == 'semantic':
                entry['similarity'] = max(entry['similarity'] if entry['similarity'] is not None else -1,
                                          float(details['similarity']))
    return out


def assess(question, evidence, rankings, settings, *, inspected=False) -> Assessment:
    visual = needs_visual(question)
    info = signals(rankings)
    strong, reasons = [], []
    for e in evidence:
        s = info.get(e['evidence_id'], {})
        if (s.get('similarity') is not None and s['similarity'] >= settings.route_min_similarity) or \
                (s.get('coverage') is not None and s['coverage'] >= settings.route_min_keyword_coverage):
            strong.append(e['evidence_id'])
    if not evidence:
        reasons.append('no_evidence')
    elif not strong:
        reasons.append('no_strong_evidence')
    # A question about what is on screen is answered only after the frames were looked at: event summaries and
    # frame similarity locate the moment but do not read the slide (E4 evaluation: answering visual questions
    # on the cheap path abstained on every one of them, run e4-eval/sim-all-v1).
    visual_ok = (not visual) or inspected
    if visual and not visual_ok:
        reasons.append('visual_question_needs_inspection')
    return Assessment(sufficient=bool(strong) and visual_ok, strong=strong, needs_visual=visual, reasons=reasons)
