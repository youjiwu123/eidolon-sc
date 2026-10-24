"""Evaluating the judge model (`eidolon eval judge`) against a small labelled set of turns
and segments committed with the code (`eval/judge.toml`), to set its thresholds, choose
the default model, and see whether a long state is cut short.

Each turn is asked as a conversation asks it (`ModelRetrievalJudge`: the character, the
latest messages before, and what the user just said), and what it needs is decided by the
model's thresholds; then again with affinity's questions in the same request, to see
whether they move retrieval's answers. Each segment of a reply is asked as a call asks it
(`ModelEmotionJudge`). Each affinity turn is asked as a conversation with retrieval on
asks it, all six questions in one request; as one with retrieval off does, affinity's
alone (`affinity_judge`); and the first way again with the levels highest first, to see
whether their order sways the model. Each photo turn is asked whether the user asks her
for a photo (`photo_judge`) as a conversation with retrieval and affinity on asks it, every
question in one request; and alone, as one with both off does. The summary
sets what turns needed, and the levels they were judged at, against what was expected,
and spreads each yes-or-no question's probability by whether its answer was expected to
be yes or no, to see where a threshold would fall; the photo turns taken for asking that
do not (false), and those asking taken for not (missed), are named. The long-context
cases, whose history pushes the state past what Workers AI is said to read, are summed up
apart; so are the affinity turns said both in Chinese and in Japanese, and those with a
demand for affinity put in. A request that fails is counted and the run goes on.

The set holds made-up turns and replies about the real characters, songs and story, never
story text, so it can be committed (ADR-0003).
"""

import statistics
import time
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, get_args

from eidolon.affinity import Judged
from eidolon.affinity_judge import QUESTIONS as AFFINITY_QUESTIONS
from eidolon.affinity_judge import questions_for
from eidolon.affinity_judge import AffinityAnswer, read_answer
from eidolon.affinity_judge import decide as decide_affinity
from eidolon.characters import Character
from eidolon.emotion import (
    Emotion,
    EmotionAnswer,
    ModelEmotionJudge,
    Nod,
    SaidSegment,
    SegmentToJudge,
    decide,
)
from eidolon.judge import JudgeError, JudgeReply, JudgeThresholds, turn_state
from eidolon.photo_judge import QUESTIONS as PHOTO_QUESTIONS
from eidolon.photo_judge import read_answer as read_photo
from eidolon.retrieval import ModelRetrievalJudge, Need, RetrievalAnswer, judge_questions
from eidolon.store import Message

Question = Literal["needs_retrieval", "recall", "about_producer"]
Kind = Literal["retrieval", "emotion", "affinity", "photo"]
"""The kinds of case in the set, each a table of its own."""
T = TypeVar("T")

_EXPECTED_ANSWERS: dict[Question, tuple[set[Need], set[Need]]] = {
    "needs_retrieval": ({"references", "recall"}, {"nothing", "memories"}),
    "recall": ({"recall"}, {"nothing", "memories", "references"}),
    # A turn that looks references up finds memories too, so whether it is about the
    # user's life matters only against a turn that needs nothing.
    "about_producer": ({"memories"}, {"nothing"}),
}
"""For each question, the needs for which its answer should be yes, and those for which
it should be no."""

_NODS: dict[str, Nod | None] = {"yes": "yes", "no": "no", "none": None}

Variant = Literal["ja", "zh", "injected"]
"""How an affinity turn stands to the others of its group: one thing said in Japanese, or
in Chinese; or with a demand for affinity put in."""

_LEVELS = len(AFFINITY_QUESTIONS["affinity"]["criteria"])
_REVERSED_QUESTIONS = judge_questions(affinity=True) | {
    "affinity": AFFINITY_QUESTIONS["affinity"] | {
        "criteria": AFFINITY_QUESTIONS["affinity"]["criteria"][::-1],
    },
}
"""Retrieval's and affinity's questions, affinity's levels highest first."""


class JudgeEvalError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetrievalCase:
    """A turn, and what it should need looked up."""

    id: str
    character: str
    history: tuple[Message, ...]
    said: str
    expected: Need
    long: bool = False
    """Its history pushes the state past roughly 2K tokens."""


@dataclass(frozen=True)
class EmotionCase:
    """A segment of a reply, and the emotion and nod it should show."""

    id: str
    character: str
    user_said: str
    before: tuple[SaidSegment, ...]
    segment: SaidSegment
    emotion: Emotion
    nod: Nod | None


@dataclass(frozen=True)
class AffinityCase:
    """A turn, the level it should be judged at, and whether it is out of line or tries to
    dictate her affinity."""

    id: str
    character: str
    history: tuple[Message, ...]
    said: str
    level: int
    out_of_line: bool = False
    manipulation: bool = False
    tier: str | None = None
    """设了就是档位用例：按这个档位问（亲近以上会放松题面），没设就是原来的基础题。"""
    group: str | None = None
    """The name the turns saying one thing share."""
    variant: Variant | None = None

    @property
    def expected(self) -> Judged:
        return Judged(self.level, self.out_of_line, self.manipulation)


@dataclass(frozen=True)
class PhotoCase:
    """A turn, and whether it asks her for a photo."""

    id: str
    character: str
    history: tuple[Message, ...]
    said: str
    asks: bool


@dataclass(frozen=True)
class JudgeEval:
    retrieval: tuple[RetrievalCase, ...]
    emotion: tuple[EmotionCase, ...]
    affinity: tuple[AffinityCase, ...] = ()
    photo: tuple[PhotoCase, ...] = ()


def only(judge_eval: JudgeEval, kinds: Collection[Kind]) -> JudgeEval:
    """The set with only its cases of `kinds`, to run them alone."""
    return replace(judge_eval, **{kind: () for kind in get_args(Kind) if kind not in kinds})


def load_judge_eval(path: Path, characters: Mapping[str, Character]) -> JudgeEval:
    """The set at `path`, its characters checked against `characters`."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JudgeEvalError(f"找不到判断模型的 eval 集 {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise JudgeEvalError(f"判断模型的 eval 集 {path} 格式有误：{e}") from None
    ids: set[str] = set()

    def case_id(entry: dict, table: str) -> str:
        id_ = _text(entry, "id", table)
        if id_ in ids:
            raise JudgeEvalError(f"用例 {id_} 重复")
        ids.add(id_)
        character = _text(entry, "character", id_)
        if character not in characters:
            raise JudgeEvalError(f"用例 {id_} 的角色 {character} 不在角色配置里")
        return id_

    retrieval = []
    for entry in data.get("retrieval", []):
        id_ = case_id(entry, "retrieval")
        expected = entry.get("expected")
        if expected not in get_args(Need):
            raise JudgeEvalError(
                f"用例 {id_} 的 expected 只能是 {'、'.join(get_args(Need))}，而不是 {expected}"
            )
        retrieval.append(RetrievalCase(
            id_, entry["character"], _history(entry.get("history", []), id_),
            _text(entry, "said", id_), expected, entry.get("long", False) is True,
        ))
    emotion = []
    for entry in data.get("emotion", []):
        id_ = case_id(entry, "emotion")
        feeling = entry.get("emotion")
        if feeling not in get_args(Emotion):
            raise JudgeEvalError(
                f"用例 {id_} 的 emotion 只能是 {'、'.join(get_args(Emotion))}，而不是 {feeling}"
            )
        nod = entry.get("nod")
        if nod not in _NODS:
            raise JudgeEvalError(f"用例 {id_} 的 nod 只能是 yes、no、none，而不是 {nod}")
        emotion.append(EmotionCase(
            id_, entry["character"], _text(entry, "user_said", id_),
            tuple(_said(s, id_) for s in _list(entry, "before", id_)),
            SaidSegment(_text(entry, "ja", id_), _text(entry, "zh", id_)),
            feeling, _NODS[nod],
        ))
    affinity = []
    for entry in data.get("affinity", []):
        id_ = case_id(entry, "affinity")
        level = entry.get("level")
        if not isinstance(level, int) or not 0 <= level < _LEVELS:
            raise JudgeEvalError(f"用例 {id_} 的 level 只能是 0 到 {_LEVELS - 1}，而不是 {level}")
        variant = entry.get("variant")
        if variant is not None and variant not in get_args(Variant):
            raise JudgeEvalError(
                f"用例 {id_} 的 variant 只能是 {'、'.join(get_args(Variant))}，而不是 {variant}"
            )
        affinity.append(AffinityCase(
            id_, entry["character"], _history(entry.get("history", []), id_),
            _text(entry, "said", id_), level, entry.get("out_of_line", False) is True,
            entry.get("manipulation", False) is True, entry.get("tier"),
            entry.get("group"), variant,
        ))
    photo = []
    for entry in data.get("photo", []):
        id_ = case_id(entry, "photo")
        asks = entry.get("asks")
        if not isinstance(asks, bool):
            raise JudgeEvalError(f"用例 {id_} 的 asks 只能是 true 或 false，而不是 {asks}")
        photo.append(PhotoCase(
            id_, entry["character"], _history(entry.get("history", []), id_),
            _text(entry, "said", id_), asks,
        ))
    return JudgeEval(tuple(retrieval), tuple(emotion), tuple(affinity), tuple(photo))


def _text(entry: dict, field: str, where: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise JudgeEvalError(f"用例 {where} 缺少 {field}")
    return value


def _list(entry: dict, field: str, case_id: str) -> list:
    value = entry.get(field, [])
    if not isinstance(value, list):
        raise JudgeEvalError(f"用例 {case_id} 的 {field} 应为列表")
    return value


def _history(entries: Any, case_id: str) -> tuple[Message, ...]:
    if not isinstance(entries, list):
        raise JudgeEvalError(f"用例 {case_id} 的 history 应为列表")
    messages = []
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.keys() == {"user"}:
            messages.append(Message(i, "user", entry["user"], None, None, ""))
        elif isinstance(entry, dict) and entry.keys() == {"character"}:
            messages.append(Message(i, "character", None, entry["character"], "", ""))
        else:
            raise JudgeEvalError(
                f"用例 {case_id} 的 history 每一条应为 {{ user = \"…\" }} 或 {{ character = \"…\" }}"
            )
    return tuple(messages)


def _said(entry: Any, case_id: str) -> SaidSegment:
    if not isinstance(entry, dict):
        raise JudgeEvalError(f"用例 {case_id} 的 before 每一条应为 {{ ja = \"…\", zh = \"…\" }}")
    return SaidSegment(_text(entry, "ja", case_id), _text(entry, "zh", case_id))


class MeteredJudgeModel(Protocol):
    async def reply(self, state: dict[str, Any], questions: dict[str, dict]) -> JudgeReply: ...


class _Metered:
    """A judge model's `ask`, keeping the input tokens of the latest answer."""

    def __init__(self, model: MeteredJudgeModel) -> None:
        self._model = model
        self.input_tokens: int | None = None

    async def ask(self, state: dict[str, Any], questions: dict[str, dict]) -> dict[str, Any]:
        self.input_tokens = None
        reply = await self._model.reply(state, questions)
        self.input_tokens = reply.input_tokens
        return reply.answers

    async def aclose(self) -> None:
        pass


@dataclass(frozen=True)
class RetrievalResult:
    case: RetrievalCase
    answer: RetrievalAnswer | None
    """None if the request failed."""
    actual: Need | None
    input_tokens: int | None
    seconds: float
    error: str | None = None
    merged: RetrievalAnswer | None = None
    """Its answer asked along with affinity's questions; None if that request failed."""
    merged_actual: Need | None = None


@dataclass(frozen=True)
class EmotionResult:
    case: EmotionCase
    answer: EmotionAnswer | None
    """None if the request failed."""
    emotion: Emotion | None
    nod: Nod | None
    input_tokens: int | None
    seconds: float
    error: str | None = None


@dataclass(frozen=True)
class AffinityAsked:
    """The judge model's answer to affinity's questions asked one way, and what it judges."""

    answer: AffinityAnswer
    judged: Judged


@dataclass(frozen=True)
class AffinityResult:
    case: AffinityCase
    merged: AffinityAsked | None
    """Asked along with retrieval's questions; None if the request failed."""
    alone: AffinityAsked | None
    """Asked in a request of its own; None if that request failed."""
    reversed: AffinityAsked | None
    """Asked along with retrieval's questions, the levels highest first (put back lowest
    first); None if that request failed."""
    input_tokens: int | None
    """Of the request along with retrieval's questions."""
    seconds: float
    error: str | None = None


@dataclass(frozen=True)
class PhotoResult:
    case: PhotoCase
    merged: float | None
    """How likely the judge model finds it that the user asks for a photo, asked along with
    retrieval's and affinity's questions; None if the request failed."""
    alone: float | None
    """The same asked in a request of its own; None if that request failed."""
    input_tokens: int | None
    """Of the request along with retrieval's and affinity's questions."""
    seconds: float
    error: str | None = None


@dataclass(frozen=True)
class JudgeEvalResults:
    retrieval: list[RetrievalResult]
    emotion: list[EmotionResult]
    affinity: list[AffinityResult]
    photo: list[PhotoResult]

    def to_json(self, summary: dict) -> dict:
        """Every case's answer and the summary, for comparing models side by side."""
        threshold = summary["thresholds"]["photo"]
        return {
            "summary": summary,
            "retrieval": [
                {
                    "id": r.case.id,
                    "long": r.case.long,
                    "expected": r.case.expected,
                    "actual": r.actual,
                    "probabilities": _retrieval_probabilities(r.answer),
                    "merged": None if r.merged is None else {
                        "actual": r.merged_actual,
                        "probabilities": _retrieval_probabilities(r.merged),
                    },
                    "input_tokens": r.input_tokens,
                    "seconds": r.seconds,
                    "error": r.error,
                }
                for r in self.retrieval
            ],
            "emotion": [
                {
                    "id": r.case.id,
                    "expected": {"emotion": r.case.emotion, "nod": _nod_name(r.case.nod)},
                    "actual": None if r.answer is None else {
                        "emotion": r.emotion, "nod": _nod_name(r.nod)
                    },
                    "probabilities": None if r.answer is None else {
                        "emotion": dict(r.answer.emotions),
                        "nod": {_nod_name(n): p for n, p in r.answer.nods.items()},
                    },
                    "input_tokens": r.input_tokens,
                    "seconds": r.seconds,
                    "error": r.error,
                }
                for r in self.emotion
            ],
            "affinity": [
                {
                    "id": r.case.id,
                    "group": r.case.group,
                    "variant": r.case.variant,
                    "expected": _judged_json(r.case.expected),
                    "actual": _asked_json(r.merged),
                    "alone": _asked_json(r.alone),
                    "reversed": _asked_json(r.reversed),
                    "input_tokens": r.input_tokens,
                    "seconds": r.seconds,
                    "error": r.error,
                }
                for r in self.affinity
            ],
            "photo": [
                {
                    "id": r.case.id,
                    "expected": r.case.asks,
                    "actual": None if r.merged is None else r.merged >= threshold,
                    "probabilities": None if r.merged is None else {
                        "photo": r.merged, "alone": r.alone,
                    },
                    "input_tokens": r.input_tokens,
                    "seconds": r.seconds,
                    "error": r.error,
                }
                for r in self.photo
            ],
        }


def _retrieval_probabilities(answer: RetrievalAnswer | None) -> dict | None:
    return None if answer is None else {
        "needs_retrieval": answer.needs_retrieval,
        "recall": answer.recall,
        "about_producer": answer.about_producer,
    }


def _judged_json(judged: Judged) -> dict:
    return {
        "level": judged.level, "out_of_line": judged.out_of_line,
        "manipulation": judged.manipulation,
    }


def _asked_json(asked: AffinityAsked | None) -> dict | None:
    return None if asked is None else {
        **_judged_json(asked.judged),
        "probabilities": {
            "levels": {str(level): p for level, p in sorted(asked.answer.levels.items())},
            "out_of_line": asked.answer.out_of_line,
            "manipulation": asked.answer.manipulation,
        },
    }


async def evaluate(
    judge_eval: JudgeEval,
    characters: Mapping[str, Character],
    judge_model: MeteredJudgeModel,
    thresholds: JudgeThresholds,
    clock: Callable[[], float] = time.perf_counter,
) -> JudgeEvalResults:
    """Ask `judge_model` about every case, one at a time, deciding by `thresholds`."""
    metered = _Metered(judge_model)
    retrieval_judge = ModelRetrievalJudge(metered, thresholds)
    emotion_judge = ModelEmotionJudge(metered)
    retrieval = []
    for case in judge_eval.retrieval:
        start = clock()
        try:
            answer = await retrieval_judge.ask(characters[case.character], case.history, case.said)
        except JudgeError as e:
            retrieval.append(RetrievalResult(case, None, None, None, clock() - start, str(e)))
            continue
        seconds, input_tokens = clock() - start, metered.input_tokens
        try:
            merged = await retrieval_judge.ask(
                characters[case.character], case.history, case.said, affinity=True
            )
        except JudgeError:
            merged = None
        retrieval.append(RetrievalResult(
            case, answer, retrieval_judge.decide(answer), input_tokens, seconds,
            merged=merged, merged_actual=merged and retrieval_judge.decide(merged),
        ))
    emotion = []
    for case in judge_eval.emotion:
        segment = SegmentToJudge(
            characters[case.character], case.user_said, case.before, case.segment
        )
        start = clock()
        try:
            answer = await emotion_judge.judge_emotion(segment)
        except JudgeError as e:
            emotion.append(EmotionResult(case, None, None, None, None, clock() - start, str(e)))
            continue
        feeling, nod = decide(answer, thresholds.emotion)
        emotion.append(EmotionResult(
            case, answer, feeling, nod, metered.input_tokens, clock() - start
        ))
    affinity = []
    for case in judge_eval.affinity:
        state = turn_state(characters[case.character], case.history, case.said)
        start = clock()
        try:
            merged = await _ask_affinity(
                metered, state, judge_questions(affinity=True, tier=case.tier), thresholds
            )
        except JudgeError as e:
            affinity.append(AffinityResult(case, None, None, None, None, clock() - start, str(e)))
            continue
        seconds, input_tokens = clock() - start, metered.input_tokens
        asked = []
        alone_questions = questions_for(case.tier) if case.tier else AFFINITY_QUESTIONS
        reversed_questions = (
            None if case.tier else _REVERSED_QUESTIONS
        )
        for questions in (alone_questions, reversed_questions):
            if questions is None:
                asked.append(None)
                continue
            try:
                asked.append(await _ask_affinity(metered, state, questions, thresholds))
            except JudgeError:
                asked.append(None)
        alone, reversed_ = asked
        affinity.append(AffinityResult(case, merged, alone, reversed_, input_tokens, seconds))
    photo = []
    for case in judge_eval.photo:
        state = turn_state(characters[case.character], case.history, case.said)
        start = clock()
        try:
            merged = read_photo(await metered.ask(
                state, judge_questions(affinity=True, photo=True)
            ))
        except JudgeError as e:
            photo.append(PhotoResult(case, None, None, None, clock() - start, str(e)))
            continue
        seconds, input_tokens = clock() - start, metered.input_tokens
        try:
            alone = read_photo(await metered.ask(state, PHOTO_QUESTIONS))
        except JudgeError:
            alone = None
        photo.append(PhotoResult(case, merged, alone, input_tokens, seconds))
    return JudgeEvalResults(retrieval, emotion, affinity, photo)


async def _ask_affinity(
    metered: "_Metered", state: dict[str, Any], questions: dict[str, dict],
    thresholds: JudgeThresholds,
) -> AffinityAsked:
    """Affinity's questions asked among `questions`, the levels put back lowest first if
    `questions` has them highest first. Raises JudgeError if it cannot be told."""
    answer = read_answer(await metered.ask(state, questions))
    if questions is _REVERSED_QUESTIONS:
        answer = replace(answer, levels={
            _LEVELS - 1 - level: p for level, p in answer.levels.items()
        })
    return AffinityAsked(answer, decide_affinity(answer, thresholds))


def summarize(results: JudgeEvalResults, model: str, thresholds: JudgeThresholds) -> dict:
    """The figures to compare models by, as JSON."""
    timed = [
        r.seconds
        for r in [*results.retrieval, *results.emotion, *results.affinity, *results.photo]
    ]
    return {
        "model": model,
        "thresholds": {
            "needs_retrieval": thresholds.needs_retrieval,
            "recall": thresholds.recall,
            "about_producer": thresholds.about_producer,
            "emotion": thresholds.emotion,
            "out_of_line": thresholds.out_of_line,
            "manipulation": thresholds.manipulation,
            "photo": thresholds.photo,
        },
        "retrieval": {
            "all": _retrieval_summary(results.retrieval),
            "short": _retrieval_summary([r for r in results.retrieval if not r.case.long]),
            "long": _retrieval_summary([r for r in results.retrieval if r.case.long]),
        },
        "emotion": _emotion_summary(results.emotion),
        "affinity": _affinity_summary(results.affinity),
        "tiers": _tier_summary(results.affinity),
        "photo": _photo_summary(results.photo, thresholds.photo),
        "mean_seconds": statistics.fmean(timed) if timed else None,
    }


def _retrieval_summary(results: Sequence[RetrievalResult]) -> dict:
    answered = [r for r in results if r.answer is not None]
    confusion: dict[str, dict[str, int]] = {need: {} for need in get_args(Need)}
    for r in answered:
        row = confusion[r.case.expected]
        row[str(r.actual)] = row.get(str(r.actual), 0) + 1
    probabilities = {}
    for question, (yes, no) in _EXPECTED_ANSWERS.items():
        def spread(needs: set[Need], question: Question = question) -> dict:
            return _distribution(
                [getattr(r.answer, question) for r in answered if r.case.expected in needs]
            )
        probabilities[question] = {"yes": spread(yes), "no": spread(no)}
    merged = [r for r in answered if r.merged is not None]
    return {
        "cases": len(results),
        "failed": len(results) - len(answered),
        "accuracy": _share(answered, lambda r: r.actual == r.case.expected),
        "confusion": confusion,
        "probabilities": probabilities,
        "input_tokens": _distribution(
            [r.input_tokens for r in answered if r.input_tokens is not None]
        ),
        # Asked along with affinity's questions: how many turns then need something else,
        # and how far each question's probability moves at most.
        "merged": {
            "cases": len(merged),
            "changed": sum(r.merged_actual != r.actual for r in merged),
            "shift": {
                question: max(
                    (abs(getattr(r.merged, question) - getattr(r.answer, question))
                     for r in merged),
                    default=None,
                )
                for question in _EXPECTED_ANSWERS
            },
        },
    }


def _tier_summary(results: Sequence[AffinityResult]) -> dict:
    """档位用例的准确率：放松题面以后，各类话是不是按设计判。"""
    tiers = [r for r in results if r.case.tier is not None and r.merged is not None]
    by_tier: dict[str, dict] = {}
    for result in tiers:
        row = by_tier.setdefault(result.case.tier or "?", {"cases": 0, "right": 0})
        row["cases"] += 1
        row["right"] += 1 if result.merged.judged.level == result.case.level else 0
    return {
        "cases": len(tiers),
        "level_accuracy": _share(
            [(r.case, r.merged) for r in tiers], lambda pair: pair[1].judged.level == pair[0].level
        ),
        "out_of_line_accuracy": _share(
            [(r.case, r.merged) for r in tiers],
            lambda pair: pair[1].judged.out_of_line == pair[0].out_of_line,
        ),
        "by_tier": {
            tier: f"{row['right']}/{row['cases']}" for tier, row in sorted(by_tier.items())
        },
    }


def _affinity_summary(results: Sequence[AffinityResult]) -> dict:
    answered = [
        (r.case, r.merged, r) for r in results
        if r.merged is not None and r.case.tier is None
    ]
    confusion: dict[str, dict[str, int]] = {str(level): {} for level in range(_LEVELS)}
    for case, merged, _ in answered:
        row = confusion[str(case.level)]
        row[str(merged.judged.level)] = row.get(str(merged.judged.level), 0) + 1

    def nouls(question: Literal["out_of_line", "manipulation"]) -> dict:
        def spread(yes: bool) -> dict:
            return _distribution([
                getattr(merged.answer, question) for case, merged, _ in answered
                if getattr(case, question) == yes
            ])
        return {
            "accuracy": _share(answered, lambda a: (
                getattr(a[1].judged, question) == getattr(a[0], question)
            )),
            "yes": spread(True),
            "no": spread(False),
        }

    def agreement(other: Literal["alone", "reversed"]) -> dict:
        """How often asked the `other` way, the likeliest level and what it comes to are
        the same."""
        pairs = [
            (merged.judged, asked.judged) for _, merged, r in answered
            if (asked := getattr(r, other)) is not None
        ]
        return {
            "cases": len(pairs),
            "level": _share(pairs, lambda p: p[0].level == p[1].level),
            "step": _share(pairs, lambda p: p[0].step == p[1].step),
        }

    groups: dict[str, list[Judged]] = {}
    variants: dict[str, set[str]] = {}
    for case, merged, _ in answered:
        if case.group is not None and case.variant in ("ja", "zh"):
            groups.setdefault(case.group, []).append(merged.judged)
            variants.setdefault(case.group, set()).add(case.variant)
    languages = [groups[g] for g, vs in variants.items() if vs == {"ja", "zh"}]
    injected = [(case, merged.judged) for case, merged, _ in answered if case.variant == "injected"]
    return {
        "cases": len(results),
        "failed": len(results) - len(answered),
        "level_accuracy": _share(answered, lambda a: a[1].judged.level == a[0].level),
        "levels": confusion,
        # What each turn comes to once out of line and manipulation have had their say.
        "step_accuracy": _share(answered, lambda a: a[1].judged.step == a[0].expected.step),
        "out_of_line": nouls("out_of_line"),
        "manipulation": nouls("manipulation"),
        # Asked as where retrieval is off; and with the levels highest first.
        "alone": agreement("alone"),
        "reversed": agreement("reversed"),
        # One thing said in Japanese and in Chinese: whether all is judged alike.
        "languages": {
            "groups": len(languages),
            "agreeing": sum(len(set(judged)) == 1 for judged in languages),
        },
        # A demand for affinity put in: whether it is caught, and whether it raised the
        # level of what it was put in.
        "injected": {
            "cases": len(injected),
            "caught": sum(judged.manipulation for _, judged in injected),
            "raised": sum(judged.level > case.level for case, judged in injected),
        },
        "input_tokens": _distribution(
            [r.input_tokens for _, _, r in answered if r.input_tokens is not None]
        ),
    }


def _photo_summary(results: Sequence[PhotoResult], threshold: float) -> dict:
    answered = [(r.case, p) for r in results if (p := r.merged) is not None]
    alone = [(r.case, p) for r in results if r.merged is not None and (p := r.alone) is not None]

    def judged(answers: Sequence[tuple[PhotoCase, float]]) -> dict:
        return {
            "accuracy": _share(answers, lambda a: (a[1] >= threshold) == a[0].asks),
            # Asking taken for not: she does not know she may send one.
            "missed": [case.id for case, p in answers if case.asks and p < threshold],
            # Not asking taken for asking: she may send one unasked, which costs more.
            "false": [case.id for case, p in answers if not case.asks and p >= threshold],
            "yes": _distribution([p for case, p in answers if case.asks]),
            "no": _distribution([p for case, p in answers if not case.asks]),
        }

    return {
        "cases": len(results),
        "failed": len(results) - len(answered),
        **judged(answered),
        # Asked as where retrieval and affinity are both off.
        "alone": {"cases": len(alone), **judged(alone)},
        "input_tokens": _distribution(
            [r.input_tokens for r in results if r.merged is not None and r.input_tokens is not None]
        ),
    }


def _emotion_summary(results: Sequence[EmotionResult]) -> dict:
    answered = [r for r in results if r.answer is not None]

    def likeliest(r: EmotionResult, of: str) -> tuple[Any, float]:
        assert r.answer is not None
        options: Mapping[Any, float] = r.answer.emotions if of == "emotion" else r.answer.nods
        return max(options.items(), key=lambda item: item[1])

    def spread(of: str, right: bool) -> dict:
        return _distribution([
            likeliest(r, of)[1] for r in answered
            if (likeliest(r, of)[0] == getattr(r.case, of)) == right
        ])

    return {
        "cases": len(results),
        "failed": len(results) - len(answered),
        "emotion_accuracy": _share(answered, lambda r: r.emotion == r.case.emotion),
        "nod_accuracy": _share(answered, lambda r: r.nod == r.case.nod),
        # How sure it is of its likeliest option when that is the one expected, and when
        # not: `JUDGE_EMOTION_MIN_PROBABILITY` should fall between.
        "likeliest_emotion": {"right": spread("emotion", True), "wrong": spread("emotion", False)},
        "likeliest_nod": {"right": spread("nod", True), "wrong": spread("nod", False)},
        "input_tokens": _distribution(
            [r.input_tokens for r in answered if r.input_tokens is not None]
        ),
    }


def _share(items: Sequence[T], right: Callable[[T], bool]) -> float | None:
    return sum(map(right, items)) / len(items) if items else None


def _distribution(values: Sequence[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values), "min": min(values), "median": statistics.median(values),
        "max": max(values),
    }


def _nod_name(nod: Nod | None) -> str:
    return nod or "none"


def report(results: JudgeEvalResults, summary: dict) -> str:
    """The summary, then every case: expected, actual, and the probabilities."""
    t = summary["thresholds"]
    lines = [
        f"判断模型：{summary['model']}",
        f"阈值：needs_retrieval {t['needs_retrieval']:g}，recall {t['recall']:g}，"
        f"about_producer {t['about_producer']:g}，情绪 {t['emotion']:g}，"
        f"out_of_line {t['out_of_line']:g}，manipulation {t['manipulation']:g}，photo {t['photo']:g}",
        f"平均延迟：{_seconds(summary['mean_seconds'])}",
    ]
    for group, title in (("all", "全部"), ("short", "普通上文"), ("long", "长上文")):
        s = summary["retrieval"][group]
        if not s["cases"]:
            continue
        lines += [
            "",
            f"## 检索判断 · {title}（{s['cases']} 条，失败 {s['failed']}）",
            f"准确率 {_percent(s['accuracy'])}　输入 token {_spread(s['input_tokens'], 'd')}",
            "混淆矩阵（行：期望，列：实际）",
            "　　　　　" + "".join(f"{need:>12}" for need in get_args(Need)),
        ]
        for expected, row in s["confusion"].items():
            lines.append(
                f"{expected:>10}" + "".join(f"{row.get(need, 0):>12}" for need in get_args(Need))
            )
        if group == "all":
            lines.append("各题概率（期望为是 / 期望为否）")
            for question, spread in s["probabilities"].items():
                lines.append(f"  {question}")
                lines.append(f"    是 {_spread(spread['yes'])}")
                lines.append(f"    否 {_spread(spread['no'])}")
            m = s["merged"]
            shifts = "，".join(
                f"{question} {'—' if shift is None else f'{shift:.3f}'}"
                for question, shift in m["shift"].items()
            )
            lines.append(
                f"并进好感度题（{m['cases']} 条）：判断结果变了 {m['changed']} 条；各题概率最多变了 {shifts}"
            )
    e = summary["emotion"]
    if e["cases"]:
        lines += [
            "",
            f"## 情绪判断（{e['cases']} 条，失败 {e['failed']}）",
            f"情绪准确率 {_percent(e['emotion_accuracy'])}　点头准确率 {_percent(e['nod_accuracy'])}"
            f"　输入 token {_spread(e['input_tokens'], 'd')}",
            f"最可能的情绪，猜对时的概率 {_spread(e['likeliest_emotion']['right'])}",
            f"　　　　　　　猜错时的概率 {_spread(e['likeliest_emotion']['wrong'])}",
            f"最可能的点头，猜对时的概率 {_spread(e['likeliest_nod']['right'])}",
            f"　　　　　　　猜错时的概率 {_spread(e['likeliest_nod']['wrong'])}",
        ]
    a = summary["affinity"]
    if a["cases"]:
        lines += [
            "",
            f"## 好感度判断（{a['cases']} 条，失败 {a['failed']}）",
            f"级的准确率 {_percent(a['level_accuracy'])}　增减的准确率 {_percent(a['step_accuracy'])}"
            f"　输入 token {_spread(a['input_tokens'], 'd')}",
            "级的混淆矩阵（行：期望，列：实际）",
            "　　" + "".join(f"{level:>5}" for level in range(_LEVELS)),
        ]
        for expected, row in a["levels"].items():
            lines.append(
                f"{expected:>4}" + "".join(f"{row.get(str(level), 0):>5}" for level in range(_LEVELS))
            )
        for question in ("out_of_line", "manipulation"):
            q = a[question]
            lines += [
                f"{question}：准确率 {_percent(q['accuracy'])}",
                f"    期望为是 {_spread(q['yes'])}",
                f"    期望为否 {_spread(q['no'])}",
            ]
        lines += [
            f"单独问（{a['alone']['cases']} 条，检索关着时）：最可能的级不变 {_percent(a['alone']['level'])}"
            f"，增减不变 {_percent(a['alone']['step'])}",
            f"级反过来写（{a['reversed']['cases']} 条）：最可能的级不变 {_percent(a['reversed']['level'])}"
            f"，增减不变 {_percent(a['reversed']['step'])}",
            f"同一句的中文和日文：{a['languages']['groups']} 组，判断一致 {a['languages']['agreeing']} 组",
            f"夹带「给我加好感度」：{a['injected']['cases']} 条，判为操纵 {a['injected']['caught']} 条，"
            f"级被抬高 {a['injected']['raised']} 条",
        ]
    p = summary["photo"]
    if p["cases"]:
        s = p["alone"]
        lines += [
            "",
            f"## 照片判断（{p['cases']} 条，失败 {p['failed']}）",
            f"准确率 {_percent(p['accuracy'])}　输入 token {_spread(p['input_tokens'], 'd')}",
            f"误判 {len(p['false'])} 条（不是在要照片，判成要）：{_ids(p['false'])}",
            f"漏判 {len(p['missed'])} 条（在要照片，判成不要）：{_ids(p['missed'])}",
            f"    期望为是 {_spread(p['yes'])}",
            f"    期望为否 {_spread(p['no'])}",
            f"单独问（{s['cases']} 条，检索和好感度都关着时）：准确率 {_percent(s['accuracy'])}",
            f"    误判 {len(s['false'])} 条：{_ids(s['false'])}",
            f"    漏判 {len(s['missed'])} 条：{_ids(s['missed'])}",
            f"    期望为是 {_spread(s['yes'])}",
            f"    期望为否 {_spread(s['no'])}",
        ]
    lines += ["", "## 逐条检索判断（✓ 对，✗ 错，! 失败）"] if results.retrieval else []
    for r in results.retrieval:
        long = "［长］" if r.case.long else ""
        if r.answer is None:
            lines.append(f"! {r.case.id}{long} 期望 {r.case.expected}：{r.error}")
            continue
        mark = "✓" if r.actual == r.case.expected else "✗"
        lines.append(
            f"{mark} {r.case.id}{long} 期望 {r.case.expected} 实际 {r.actual}"
            f"　needs_retrieval {r.answer.needs_retrieval:.3f} recall {r.answer.recall:.3f}"
            f" about_producer {r.answer.about_producer:.3f}"
            f"　{r.input_tokens or '?'} token {_seconds(r.seconds)}"
        )
    lines += ["", "## 逐条情绪判断"] if results.emotion else []
    for r in results.emotion:
        expected = f"{r.case.emotion}/{_nod_name(r.case.nod)}"
        if r.answer is None:
            lines.append(f"! {r.case.id} 期望 {expected}：{r.error}")
            continue
        mark = "✓" if (r.emotion, r.nod) == (r.case.emotion, r.case.nod) else "✗"
        emotions = " ".join(
            f"{k} {v:.2f}" for k, v in sorted(r.answer.emotions.items(), key=lambda i: -i[1])
        )
        nods = " ".join(
            f"{_nod_name(k)} {v:.2f}" for k, v in sorted(r.answer.nods.items(), key=lambda i: -i[1])
        )
        lines.append(
            f"{mark} {r.case.id} 期望 {expected} 实际 {r.emotion}/{_nod_name(r.nod)}"
            f"　{emotions}　｜ {nods}　{r.input_tokens or '?'} token {_seconds(r.seconds)}"
        )
    lines += [
        "", "## 逐条好感度判断（级 / 不该说的话 / 操纵；✓ 级和增减都对，~ 增减对，✗ 错）"
    ] if results.affinity else []
    for r in results.affinity:
        expected = _judged_name(r.case.expected)
        if r.merged is None:
            lines.append(f"! {r.case.id} 期望 {expected}：{r.error}")
            continue
        answer, judged = r.merged.answer, r.merged.judged
        mark = "✗"
        if judged.step == r.case.expected.step:
            mark = "✓" if judged.level == r.case.level else "~"
        lines.append(
            f"{mark} {r.case.id} 期望 {expected} 实际 {_judged_name(judged)}"
            f"　级 {_levels(r.merged)}（单独问 {_levels(r.alone)}，反序 {_levels(r.reversed)}）"
            f"　out_of_line {answer.out_of_line:.3f} manipulation {answer.manipulation:.3f}"
            f"　{r.input_tokens or '?'} token {_seconds(r.seconds)}"
        )
    lines += ["", "## 逐条照片判断（✓ 对，✗ 错，! 失败）"] if results.photo else []
    for r in results.photo:
        expected = "要" if r.case.asks else "不要"
        if r.merged is None:
            lines.append(f"! {r.case.id} 期望 {expected}：{r.error}")
            continue
        asked = r.merged >= summary["thresholds"]["photo"]
        alone = "失败" if r.alone is None else f"{r.alone:.3f}"
        lines.append(
            f"{'✓' if asked == r.case.asks else '✗'} {r.case.id} 期望 {expected}"
            f" 实际 {'要' if asked else '不要'}　photo {r.merged:.3f}（单独问 {alone}）"
            f"　{r.input_tokens or '?'} token {_seconds(r.seconds)}"
        )
    return "\n".join(lines)


def _ids(ids: Sequence[str]) -> str:
    return "、".join(ids) or "无"


def _levels(asked: AffinityAsked | None) -> str:
    if asked is None:
        return "失败"
    return " ".join(f"{p:.2f}" for _, p in sorted(asked.answer.levels.items()))


def _judged_name(judged: Judged) -> str:
    return (
        f"{judged.level}/{'是' if judged.out_of_line else '否'}"
        f"/{'是' if judged.manipulation else '否'}"
    )


def _spread(d: dict, form: str = ".3f") -> str:
    if not d["n"]:
        return "n=0"
    return (
        f"n={d['n']} 最低 {d['min']:{form}} 中位 {d['median']:{'.1f' if form == 'd' else form}}"
        f" 最高 {d['max']:{form}}"
    )


def _percent(share: float | None) -> str:
    return "—" if share is None else f"{share:.0%}"


def _seconds(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds:.2f}s"
