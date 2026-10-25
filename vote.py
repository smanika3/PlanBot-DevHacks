"""Approval voting. Each person approves every option they could live with."""


def tally(approvals: dict) -> tuple:
    """approvals = {option_id: set(person_id)}. Returns (winners, counts). More than one winner means a tie."""
    counts = {o: len(p) for o, p in approvals.items()}
    best = max(counts.values(), default=0)
    return ([o for o, c in counts.items() if c == best] if best else []), counts


def all_voted(participants: set, approvals: dict) -> bool:
    """A segment locks once everyone in it has approved at least one option."""
    voters = set().union(*approvals.values()) if approvals else set()
    return bool(participants) and participants <= voters
