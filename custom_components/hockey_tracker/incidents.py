"""Disallowed-goal and major-incident detection, shared by both coordinators.

Disallowed goals
----------------
No feed says "this goal was disallowed". HockeyTech simply removes the goal from
the game summary; the NHL removes it from the landing/play-by-play but also logs
the coach's challenge or video review as a stoppage, which supplies the reason.

A goal also drops out of the feed for harmless reasons (scorekeeper deleting and
re-entering it, a failed summary fetch), so a goal is only declared disallowed
when two independent feeds agree:

* the goal has been missing from the event feed for MISSING_POLLS_TO_DISALLOW
  consecutive live polls, and
* the scoreboard score for that team went down — the scoreboard never goes down
  otherwise. This is tracked as a high-water mark of (scoreboard score + goals
  already flagged) per side, so a later disallowed goal is still caught after
  the team has scored again.

Major incidents
---------------
Fights, ejections (game misconduct, match, gross misconduct), other major
penalties and penalty shots, classified from the penalty data already fetched.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base_coordinator import (
    GOAL_MATCH_TOLERANCE_SECONDS,
    _goal_key,
    _period,
    time_to_seconds,
)

# Consecutive live polls a goal must be absent before it can be declared disallowed
MISSING_POLLS_TO_DISALLOW = 2

# How far (seconds) an NHL review stoppage can be from the goal it relates to
REVIEW_MATCH_SECONDS = 30

INCIDENT_FIGHT = "fight"
INCIDENT_EJECTION = "ejection"
INCIDENT_MAJOR = "major"
INCIDENT_PENALTY_SHOT = "penalty_shot"

# Most severe first — drives the notification title order
_INCIDENT_LABELS = {
    INCIDENT_EJECTION: "Ejection",
    INCIDENT_FIGHT: "Fight",
    INCIDENT_MAJOR: "Major penalty",
    INCIDENT_PENALTY_SHOT: "Penalty shot",
}

# NHL challenge / review codes (after the "chlg-hm-" / "chlg-vis-" prefix)
_REVIEW_REASONS = {
    "goal-interference": "goaltender interference",
    "off-side": "offside",
    "offside": "offside",
    "missed-stoppage": "missed stoppage",
    "high-stick": "high stick",
    "kicked-in": "kicked in",
    "video-review": "video review",
}


# ----------------------------------------------------------------------
# Penalty classification
# ----------------------------------------------------------------------


def _minutes(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def classify_ht_penalty(description: str | None, minutes: Any) -> str | None:
    """Incident kind for a HockeyTech penalty, or None for a routine one.

    Descriptions are free text that varies by league and scorekeeper, e.g.
    "Fighting - Major", "Game Misconduct - Second Fight", "Match - Roughing",
    "Cross-Checking Major", "Elbowing - major".
    """
    desc = (description or "").lower()
    if desc.startswith("match") or "match penalty" in desc:
        return INCIDENT_EJECTION
    if "game misconduct" in desc or "gross misconduct" in desc:
        return INCIDENT_EJECTION
    mins = _minutes(minutes)
    if "fight" in desc and mins >= 5:
        return INCIDENT_FIGHT
    if "penalty shot" in desc:
        return INCIDENT_PENALTY_SHOT
    if "major" in desc or mins == 5:
        return INCIDENT_MAJOR
    return None


def classify_nhl_penalty(penalty_type: str | None, desc_key: str | None) -> str | None:
    """Incident kind for an NHL landing penalty (type MIN/MAJ/MIS/GAM/MAT/PS/BEN)."""
    ptype = (penalty_type or "").upper()
    key = (desc_key or "").lower()
    if ptype in ("MAT", "GAM", "GRO") or "match" in key or "game-misconduct" in key or "gross-misconduct" in key:
        return INCIDENT_EJECTION
    if ptype == "PS" or key.startswith("ps-") or "penalty-shot" in key:
        return INCIDENT_PENALTY_SHOT
    if key.startswith("fighting"):
        return INCIDENT_FIGHT
    if ptype == "MAJ":
        return INCIDENT_MAJOR
    return None


def ht_penalty_shots(summary: dict, is_tracked: Callable[[str], bool]) -> list[dict]:
    """Incident items for the penalty shots in a HockeyTech gameSummary."""
    items: list[dict] = []
    shots = summary.get("penaltyShots") or {}
    for side in ("homeTeam", "visitingTeam"):
        for shot in shots.get(side) or []:
            team = shot.get("shooter_team") or {}
            shooter = shot.get("shooter") or {}
            try:
                period = int((shot.get("period") or {}).get("id") or 0)
            except (TypeError, ValueError):
                period = 0
            items.append({
                "kind": INCIDENT_PENALTY_SHOT,
                "period": period,
                "time": shot.get("time", ""),
                "team_abbrev": team.get("abbreviation", ""),
                "is_tracked_team": is_tracked(str(team.get("id", ""))),
                "player_name": f"{shooter.get('firstName', '')} {shooter.get('lastName', '')}".strip(),
                "is_goal": shot.get("isGoal") in (True, 1, "1", "true"),
            })
    return items


def collect_incidents(data: dict) -> list[dict]:
    """All incident items for a game: flagged penalties plus HockeyTech penalty shots.

    A major given to a player who is ejected at the same time is dropped — the
    ejection line already covers it.
    """
    items = [
        {
            "kind": e["incident"],
            "period": e.get("period"),
            "time": e.get("time", ""),
            "team_abbrev": e.get("team_abbrev", ""),
            "is_tracked_team": e.get("is_tracked_team"),
            "player_name": e.get("player_name", ""),
            "description": e.get("description", ""),
        }
        for e in data.get("game_events") or []
        if e.get("type") == "penalty" and e.get("incident")
    ]
    items += data.get("_penalty_shots") or []

    def _who(i: dict) -> tuple:
        return (_period(i), time_to_seconds(i.get("time")), i.get("team_abbrev"), i.get("player_name"))

    ejected = {_who(i) for i in items if i["kind"] == INCIDENT_EJECTION}
    return [i for i in items if not (i["kind"] == INCIDENT_MAJOR and _who(i) in ejected)]


def incident_key(item: dict) -> tuple:
    return (
        _period(item), item.get("kind", ""), str(item.get("team_abbrev") or ""),
        str(item.get("player_name") or ""), time_to_seconds(item.get("time")),
    )


def _when(item: dict) -> str:
    return f"P{item.get('period', '')} {item.get('time', '')}".strip()


def format_incidents(items: list[dict]) -> tuple[str, str]:
    """Notification (title, message) for newly seen incidents, one line each.

    Fighting majors at the same time are combined into one "A vs B" line.
    """
    lines: list[str] = []
    fights: dict[tuple, list[dict]] = {}
    for item in sorted(items, key=lambda i: (_period(i), time_to_seconds(i.get("time")))):
        if item["kind"] == INCIDENT_FIGHT:
            fights.setdefault((_period(item), time_to_seconds(item.get("time"))), []).append(item)
    fight_lines_done: set[tuple] = set()

    for item in sorted(items, key=lambda i: (_period(i), time_to_seconds(i.get("time")))):
        kind = item["kind"]
        who = f"{item.get('player_name') or 'Unknown'} ({item.get('team_abbrev', '')})"
        if kind == INCIDENT_FIGHT:
            slot = (_period(item), time_to_seconds(item.get("time")))
            if slot in fight_lines_done:
                continue
            fight_lines_done.add(slot)
            # Tracked team's fighter first, then alphabetical by team
            ordered = sorted(fights[slot], key=lambda f: (not f.get("is_tracked_team"), f.get("team_abbrev", "")))
            fighters = " vs ".join(
                f"{f.get('player_name') or 'Unknown'} ({f.get('team_abbrev', '')})" for f in ordered
            )
            lines.append(f"Fight: {fighters} — {_when(item)}")
        elif kind == INCIDENT_EJECTION:
            lines.append(f"Ejected: {who} — {item.get('description', '')} — {_when(item)}")
        elif kind == INCIDENT_MAJOR:
            lines.append(f"Major: {who} — {item.get('description', '')} — {_when(item)}")
        elif kind == INCIDENT_PENALTY_SHOT:
            if "is_goal" in item:  # HockeyTech: the shot itself, result known
                result = "scores!" if item["is_goal"] else "no goal"
                lines.append(f"Penalty shot: {who} — {result} — {_when(item)}")
            else:  # NHL: the penalty that awarded it
                reason = str(item.get("description", ""))
                reason = reason[3:] if reason.lower().startswith("ps ") else reason
                lines.append(f"Penalty shot awarded — {reason} by {who} — {_when(item)}".replace("  ", " "))

    kinds = {i["kind"] for i in items}
    labels = [label for kind, label in _INCIDENT_LABELS.items() if kind in kinds]
    title = labels[0] + "".join(f" & {label.lower()}" for label in labels[1:])
    return f"Hockey: {title}", "\n".join(lines)


# ----------------------------------------------------------------------
# NHL reviews
# ----------------------------------------------------------------------


def nhl_reviews(playbyplay: dict | None, home_abbrev: str, away_abbrev: str) -> list[dict]:
    """Coach's challenges and video reviews logged as play-by-play stoppages.

    Codes look like "chlg-hm-goal-interference" (home challenge),
    "chlg-vis-off-side" (visitor), "chlg-league-..." (league-initiated) or plain
    "video-review"; they can be in reason or secondaryReason.
    """
    reviews: list[dict] = []
    for play in (playbyplay or {}).get("plays") or []:
        if play.get("typeDescKey") != "stoppage":
            continue
        details = play.get("details") or {}
        codes = [c for c in (details.get("secondaryReason"), details.get("reason")) if c]
        code = next((c for c in codes if c.startswith("chlg-")), None)
        challenger = None
        if code:
            who, _, why = code[5:].partition("-")
            challenger = {"hm": home_abbrev, "vis": away_abbrev, "league": "league"}.get(who)
            if challenger is None:  # unknown prefix — keep the whole code as the reason
                why = code[5:]
        elif "video-review" in codes:
            why = "video-review"
        else:
            continue
        reviews.append({
            "period": (play.get("periodDescriptor") or {}).get("number", 0),
            "time": play.get("timeInPeriod", ""),
            "reason": _REVIEW_REASONS.get(why, why.replace("-", " ")),
            "challenged_by": challenger,
        })
    return reviews


def disallowed_reason_text(goal: dict) -> str:
    reason = goal.get("reason")
    by = goal.get("challenged_by")
    if by == "league":
        return f"Overturned on league review ({reason})" if reason else "Overturned on league review"
    if by:
        return f"Overturned on {by} challenge ({reason})" if reason else f"Overturned on {by} challenge"
    if reason:
        return f"Overturned on {reason}"
    return "Taken off the board"


# ----------------------------------------------------------------------
# Disallowed goal tracking
# ----------------------------------------------------------------------


class DisallowedGoalTracker:
    """Per-game memory of the goals seen on the board; flags those taken off it."""

    def __init__(self) -> None:
        self.game_id: str | None = None
        self._reset()

    def _reset(self) -> None:
        self._active: list[dict] = []      # {"goal", "side", "misses"}
        self._disallowed: list[dict] = []  # {"goal", "side"} — goal carries reason fields
        self._peak: dict[str, int] = {}    # side → high-water mark of score + flagged

    @property
    def goals(self) -> list[dict]:
        """Disallowed goals for the current game, newest first."""
        return sorted((e["goal"] for e in self._disallowed), key=_goal_key, reverse=True)

    def update(
        self,
        game_id: str,
        live: bool,
        goals: list[dict],
        feed_scores: dict[str, int | None],
        side_of: Callable[[dict], str | None],
        reviews: list[dict] | None = None,
    ) -> list[dict]:
        """Feed one poll; return goals newly declared disallowed.

        feed_scores maps "home"/"away" to the scoreboard score before any
        reconciliation with the goal list. Nothing is tracked outside live play.
        """
        if game_id != self.game_id:
            self.game_id = game_id
            self._reset()
        if not live:
            return []

        unmatched = sorted(goals, key=_goal_key)

        def _claim(entry_goal: dict) -> dict | None:
            period, team, seconds = _goal_key(entry_goal)
            candidates = [
                g for g in unmatched
                if _goal_key(g)[:2] == (period, team)
                and abs(_goal_key(g)[2] - seconds) <= GOAL_MATCH_TOLERANCE_SECONDS
            ]
            if not candidates:
                return None
            best = min(candidates, key=lambda g: abs(_goal_key(g)[2] - seconds))
            unmatched.remove(best)
            return best

        for entry in self._active:
            match = _claim(entry["goal"])
            if match is not None:
                entry["goal"], entry["misses"] = match, 0
            else:
                entry["misses"] += 1
        # A flagged goal that comes back was not disallowed after all
        for entry in list(self._disallowed):
            match = _claim(entry["goal"])
            if match is not None:
                self._disallowed.remove(entry)
                self._active.append({"goal": match, "side": entry["side"], "misses": 0})
        for goal in unmatched:
            self._active.append({"goal": goal, "side": side_of(goal), "misses": 0})

        newly: list[dict] = []
        for side, score in feed_scores.items():
            if score is None:
                continue
            flagged = sum(1 for e in self._disallowed if e["side"] == side)
            total = score + flagged
            peak = max(self._peak.get(side, total), total)
            self._peak[side] = peak
            pending = peak - total
            if pending <= 0:
                continue
            candidates = sorted(
                (e for e in self._active if e["side"] == side and e["misses"] >= MISSING_POLLS_TO_DISALLOW),
                key=lambda e: _goal_key(e["goal"]),
                reverse=True,
            )
            for entry in candidates[:pending]:
                self._active.remove(entry)
                goal = {**entry["goal"], "type": "goal_disallowed", **_match_review(entry["goal"], reviews)}
                self._disallowed.append({"goal": goal, "side": side})
                newly.append(goal)
        return newly


def _match_review(goal: dict, reviews: list[dict] | None) -> dict:
    period, _, seconds = _goal_key(goal)
    close = [
        r for r in reviews or []
        if _period(r) == period and abs(time_to_seconds(r.get("time")) - seconds) <= REVIEW_MATCH_SECONDS
    ]
    if not close:
        return {"reason": None, "challenged_by": None}
    best = min(close, key=lambda r: abs(time_to_seconds(r.get("time")) - seconds))
    return {"reason": best["reason"], "challenged_by": best["challenged_by"]}
