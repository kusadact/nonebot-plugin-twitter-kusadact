TIMELINE_SEEN_LIMIT = 50


def normalize_tweet_href(href: str) -> str:
    return href.split("#", 1)[0]


def build_timeline_signature(href: str) -> str:
    parts = normalize_tweet_href(href).strip("/").split("/")
    if len(parts) >= 3 and parts[1] == "status":
        return f"status:{parts[2]}"
    return normalize_tweet_href(href)


def normalize_timeline_signature(signature: str) -> str:
    if signature.startswith(("tweet:", "retweet:")):
        signature = signature.split(":", 1)[1]
    return build_timeline_signature(signature)


def get_recent_timeline_signatures(entries: list[dict], limit: int = TIMELINE_SEEN_LIMIT) -> list[str]:
    return [entry["signature"] for entry in entries[:limit]]


def get_new_timeline_entries(entries: list[dict], seen_signatures: list[str]) -> list[dict]:
    if not entries or not seen_signatures:
        return []

    current_signatures = {normalize_timeline_signature(entry["signature"]) for entry in entries}
    seen_signature_set = {normalize_timeline_signature(signature) for signature in seen_signatures}
    if not current_signatures.intersection(seen_signature_set):
        return []

    new_entries = []
    for entry in entries:
        if normalize_timeline_signature(entry["signature"]) in seen_signature_set:
            break
        new_entries.append(entry)

    new_entries.reverse()
    return new_entries
