#!/usr/bin/env python3
"""
Dry-run: shows what the auto-uploader WOULD upload, without touching Instagram.

Run:
    source venv/bin/activate
    python dry_run.py
"""

import sys
from auto_uploader.config import AppConfig
from auto_uploader.sources.twitter_cli import TwitterCliSource
from auto_uploader.state import StateStore


def main():
    config = AppConfig.load()
    state  = StateStore(db_path=config.service.db_path)

    if not config.twitter.enabled:
        print("Twitter source disabled in config. Nothing to check.")
        sys.exit(0)

    source = TwitterCliSource(config.twitter.model_dump())
    limit  = getattr(config.twitter, "initial_load", 200) or 200

    print(f"Fetching up to {limit} posts from @{source.username} ...")
    try:
        posts = source.fetch_new_posts(limit=limit)
    except Exception as e:
        print(f"ERROR fetching posts: {e}")
        sys.exit(1)

    print(f"Fetched {len(posts)} total posts.\n")

    # Same filter logic as engine.check()
    unseen = [p for p in posts if not state.is_seen(p.id) and p.has_media]
    unseen.sort(key=lambda p: int(p.id) if p.id.isdigit() else 0)

    if not unseen:
        print("Nothing to upload — all posts already marked as seen in DB.")
        return

    print(f"{'#':<5} {'Post ID':<22} {'Media':<6} URL")
    print("-" * 80)
    for i, post in enumerate(unseen, 1):
        author = post.author_username or "unknown"
        url    = post.url or f"https://x.com/{author}/status/{post.id}"
        n_media = len(post.media_items)
        print(f"{i:<5} {post.id:<22} {n_media:<6} {url}")

    print(f"\nTotal would-upload: {len(unseen)} post(s)")


if __name__ == "__main__":
    main()
