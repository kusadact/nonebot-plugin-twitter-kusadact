"""Persist discovered tweets before fetching/sending, and acknowledge only successes."""

import logging

from .timeline import (
    TIMELINE_SEEN_LIMIT,
    get_new_timeline_entries,
    get_recent_timeline_signatures,
    normalize_timeline_signature,
)

logger = logging.getLogger(__name__)


def acknowledge_tweet(state, user_name, entry):
    if user_name not in state:
        return
    user = state[user_name]
    signature = normalize_timeline_signature(entry["signature"])
    user["pending_tweets"] = [item for item in user.get("pending_tweets", [])
                              if normalize_timeline_signature(item["signature"]) != signature]
    seen = [signature, *user.get("timeline_seen", [])]
    user["timeline_seen"] = list(dict.fromkeys(map(normalize_timeline_signature, seen)))[:TIMELINE_SEEN_LIMIT]
    user.get("pending_deliveries", {}).pop(str(entry["tweet_id"]), None)
    try:
        user["since_id"] = str(max(int(user.get("since_id", "0")), int(entry["tweet_id"])))
    except (ValueError, TypeError):
        pass


async def process_timeline(user_name, entries, browser, *, read_state, update_state, fetch_tweet, deliver_tweet, enqueue_only=False):
    user = read_state().get(user_name)
    if user is None:
        return True
    seen = user.get("timeline_seen", [])
    if "timeline_seen" not in user and not user.get("pending_tweets"):
        # A new subscription starts at the current timeline, without replaying old tweets.
        if entries:
            def initialize(state):
                if user_name in state and "timeline_seen" not in state[user_name]:
                    state[user_name]["timeline_seen"] = get_recent_timeline_signatures(entries)

            update_state(initialize)
        return True

    new_entries = get_new_timeline_entries(entries, seen)

    def enqueue(state):
        if user_name not in state:
            return
        queue = state[user_name].setdefault("pending_tweets", [])
        queued = {normalize_timeline_signature(entry["signature"]) for entry in queue}
        completed = {normalize_timeline_signature(sig) for sig in state[user_name].get("timeline_seen", [])}
        for entry in new_entries:
            signature = normalize_timeline_signature(entry["signature"])
            if signature not in queued and signature not in completed:
                queue.append(dict(entry))
                queued.add(signature)

    update_state(enqueue)
    if enqueue_only:
        return True
    success = True
    for entry in list(read_state().get(user_name, {}).get("pending_tweets", [])):
        latest = read_state()
        if user_name not in latest:
            return True
        try:
            tweet = await fetch_tweet(browser, entry["source_user_name"], entry["tweet_id"])
            if entry.get("is_retweet"):
                tweet["is_retweet"] = True
                text = f"@{user_name} 转帖了 @{entry['source_user_name']}"
                tweet["text"] = [text, *tweet.get("text", [])]
            if not await deliver_tweet(tweet, user_name, entry["tweet_id"], latest):
                success = False
                continue
        except Exception:
            logger.warning("推文 %s/status/%s 保留待重试", user_name, entry["tweet_id"], exc_info=True)
            success = False
            continue

        update_state(lambda state: acknowledge_tweet(state, user_name, entry))
    return success
