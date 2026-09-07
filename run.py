#!/usr/bin/env python3
"""CLI entrypoint for Instagram Auto Uploader."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional

from auto_uploader.config import AppConfig
from auto_uploader.engine import AutoUploaderEngine
from auto_uploader.models import MediaItem, MediaType, Post
from auto_uploader.plugins.anime_crop import AnimeCropPlugin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run")


def cmd_start(args: argparse.Namespace) -> None:
    """Start continuous daemon service."""
    cfg = AppConfig.load(args.config)
    if getattr(args, "upload_all", False):
        cfg.service.initial_skip = False
    engine = AutoUploaderEngine(cfg)
    logger.info("Starting Instagram Auto Uploader daemon...")
    engine.start()


def cmd_watch(args: argparse.Namespace) -> None:
    """Watch incoming/ folder for drag-and-drop uploads."""
    cfg = AppConfig.load(args.config)
    # Ensure local source is active
    cfg.local.enabled = True
    watch_path = Path(cfg.local.watch_dir).resolve()
    print(f"\n👀 Watching folder: {watch_path}")
    print("Drag and drop anime images, videos, or album folders into 'incoming/'.")
    print(f"Checking every {cfg.service.check_interval} seconds. Press Ctrl+C to stop.\n")
    engine = AutoUploaderEngine(cfg)
    engine.start()


def cmd_check(args: argparse.Namespace) -> None:
    """Execute a single check cycle."""
    cfg = AppConfig.load(args.config)
    if getattr(args, "upload_all", False):
        cfg.service.initial_skip = False
    engine = AutoUploaderEngine(cfg)
    result = asyncio.run(engine.check())
    print("\n--- Check Summary ---")
    print(f"Total posts retrieved : {result['total']}")
    print(f"New posts found        : {result['found']}")
    print(f"Uploaded to Instagram  : {result['uploaded']}")
    print(f"Baseline established   : {'Yes' if result['baseline'] else 'No'}\n")


def cmd_test(args: argparse.Namespace) -> None:
    """Test connections to Instagram and Sources."""
    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)

    print("\n=== Connection Tests ===")

    # Test Sources
    for src in engine.sources:
        print(f"Testing source '{src.name}'...", end=" ", flush=True)
        ok = src.test_connection()
        print("OK" if ok else "FAILED")

    # Test Instagram
    print("Testing Instagram login...", end=" ", flush=True)
    ig_ok = engine.uploader.test_connection()
    print("OK" if ig_ok else "FAILED")
    print()


def cmd_crop(args: argparse.Namespace) -> None:
    """Test the anime face detector and smart cropping on a standalone image."""
    img_path = Path(args.image)
    if not img_path.is_file():
        logger.error("Image file not found: %s", img_path)
        sys.exit(1)

    out_path = Path(args.output) if args.output else img_path.with_name(f"{img_path.stem}_cropped.jpg")

    plugin_cfg = {
        "enabled": True,
        "target_aspect_ratio": args.ratio,
        "model_type": args.model,
        "device": args.device,
        "fallback_mode": args.fallback,
    }

    cropper = AnimeCropPlugin(plugin_cfg)
    cropper.on_init()

    item = MediaItem(
        url=None,
        media_type=MediaType.PHOTO,
        local_path=img_path,
        original_path=img_path,
    )
    dummy_post = Post(id="test_crop", source_name="cli_test")

    processed = cropper.process_media(item, dummy_post)

    if processed.local_path and processed.local_path.is_file():
        if processed.local_path != out_path:
            import shutil
            shutil.copyfile(processed.local_path, out_path)
        print("\n--- Smart Crop Complete ---")
        print(f"Original image : {img_path}")
        print(f"Output image   : {out_path}")
        print(f"Dimensions     : {processed.width}x{processed.height}")
        print(f"Faces detected : {processed.metadata.get('anime_faces_detected', 0)}")
        boxes = processed.metadata.get("anime_face_boxes", [])
        for i, b in enumerate(boxes):
            print(f"  Face {i+1}: [x1={b[0]:.1f}, y1={b[1]:.1f}, x2={b[2]:.1f}, y2={b[3]:.1f}, score={b[4]:.2f}]")
        print()
    else:
        logger.error("Failed to process image.")


def cmd_stats(args: argparse.Namespace) -> None:
    """Print service statistics."""
    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)
    stats = engine.state.get_stats()
    initialized = engine.state.is_initialized()
    uploads_24h = engine.state.get_uploads_in_last_hours(24)
    last_upload = engine.state.get_last_upload_time()

    print("\n--- Statistics ---")
    print(f"Posts checked       : {stats.get('checked', 0)}")
    print(f"Posts published     : {stats.get('published', 0)}")
    print(f"Errors recorded     : {stats.get('errors', 0)}")
    print(f"Baseline ready      : {'Yes' if initialized else 'No'}")
    print(f"Uploads in last 24h : {uploads_24h}")

    print("\n--- Rate Limit & Scheduling ---")
    rate_status = "ENABLED" if cfg.service.rate_limit_enabled else "DISABLED"
    print(f"Rate Limiting       : {rate_status}")
    if cfg.service.rate_limit_enabled:
        print(f"Max posts per day   : {cfg.service.max_posts_per_day} (Used: {uploads_24h}/{cfg.service.max_posts_per_day})")
        print(f"Min post interval   : {cfg.service.min_post_interval}s ({cfg.service.min_post_interval // 60}m)")
        if last_upload:
            import time
            elapsed = int(time.time() - last_upload)
            print(f"Last upload elapsed : {elapsed}s ago")
    print()


def cmd_reset(args: argparse.Namespace) -> None:
    """Reset history, seen posts, old Instagram session, and optionally restore processed files."""
    import shutil
    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)

    print("\n--- Resetting Uploader State ---")

    # 1. Reset SQLite state database
    engine.state.reset_history()
    print("✅ Seen posts database reset (post history cleared).")

    # 2. Delete cached Instagram session
    session_file = Path(cfg.instagram.session_file)
    if session_file.is_file():
        try:
            session_file.unlink()
            print(f"✅ Removed old Instagram session ({session_file}). Next run logs into new account fresh.")
        except Exception as e:
            print(f"⚠️ Could not delete session file: {e}")
    else:
        print("ℹ️ No cached Instagram session file found.")

    # 3. Restore processed files back to incoming
    if getattr(args, "restore_files", False):
        proc_dir = Path(cfg.local.processed_dir)
        watch_dir = Path(cfg.local.watch_dir)
        watch_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        if proc_dir.is_dir():
            for item in list(proc_dir.iterdir()):
                if item.is_dir():
                    # Date directories e.g. 2026-09-06
                    for sub in list(item.iterdir()):
                        dest = watch_dir / sub.name
                        if dest.exists():
                            dest = watch_dir / f"{sub.stem}_restored{sub.suffix}"
                        shutil.move(str(sub), str(dest))
                        count += 1
                    try:
                        item.rmdir()
                    except Exception:
                        pass
                else:
                    dest = watch_dir / item.name
                    shutil.move(str(item), str(dest))
                    count += 1
        print(f"✅ Restored {count} file(s)/folder(s) from '{proc_dir}' back into '{watch_dir}'.")
    else:
        print("💡 Note: To move already uploaded files from 'data/processed/' back into 'incoming/', run:")
        print("   python run.py reset --restore-files")

    print()


def cmd_fix_captions(args: argparse.Namespace) -> None:
    """Repair missing Instagram captions for recent posts where Instagram stripped them."""
    import sqlite3
    import time
    from datetime import datetime, timezone

    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)

    print("\n--- Repairing Missing Instagram Captions ---")

    # 1. Connect to Instagram
    uploader = engine.uploader
    try:
        cl = uploader._ensure_client()
    except Exception as e:
        print(f"❌ Instagram authentication failed: {e}")
        return

    # 2. Fetch user's recent media
    user_id = cl.user_id_from_username(cfg.instagram.username)
    limit = getattr(args, "limit", 30)
    print(f"Fetching up to {limit} recent posts from @{cfg.instagram.username}...")
    medias = cl.user_medias(user_id, amount=limit)

    # 3. Fetch tweets from Twitter source if enabled
    twitter_posts = {}
    twitter_source = next((s for s in engine.sources if s.name == "twitter"), None)
    if twitter_source:
        print("Fetching recent Twitter posts for caption recovery...")
        try:
            posts = twitter_source.fetch_new_posts(limit=max(40, limit))
            twitter_posts = {p.id: p for p in posts}
            print(f"Loaded {len(twitter_posts)} Twitter posts.")
        except Exception as e:
            print(f"⚠️ Could not fetch from Twitter source: {e}")

    # 4. Load seen_posts timestamp map from database
    db_posts = []
    con = sqlite3.connect(cfg.service.db_path)
    cur = con.cursor()
    try:
        db_posts = cur.execute(
            "SELECT post_id, source, seen_at FROM seen_posts ORDER BY seen_at DESC"
        ).fetchall()
    except Exception as e:
        print(f"⚠️ Database query failed: {e}")
    finally:
        con.close()

    # 5. Identify media with missing captions and fix them
    fixed_count = 0
    skipped_count = 0
    force = getattr(args, "force", False)

    for m in medias:
        current_caption = (m.caption_text or "").strip()
        if current_caption and not force:
            continue

        # Match to DB post by timestamp
        m_ts = m.taken_at.timestamp()
        matched_pid = None
        min_diff = float("inf")
        for pid, source, seen_at_str in db_posts:
            try:
                dt = datetime.strptime(seen_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                diff = abs(m_ts - dt.timestamp())
                if diff < 30 and diff < min_diff:
                    min_diff = diff
                    matched_pid = pid
            except Exception:
                continue

        if not matched_pid:
            print(f"⚠️ IG PK {m.pk} (Code: {m.code}): No matching post found in database (diff > 30s).")
            skipped_count += 1
            continue

        post_obj = twitter_posts.get(matched_pid)
        if not post_obj:
            print(f"⚠️ IG PK {m.pk} matched post {matched_pid}, but tweet data not found in source.")
            skipped_count += 1
            continue

        # Generate spam-safe caption through plugin pipeline
        new_caption = engine.plugin_manager.apply_caption_pipeline(
            post_obj.caption or post_obj.text,
            post_obj,
        )
        print(f"\nRepairing IG PK {m.pk} (Code: {m.code}) <- Post {matched_pid} (Artist: {post_obj.author_username})...")
        first_line = new_caption.splitlines()[0] if new_caption else ""
        print(f"Caption: {first_line}")

        try:
            cl.media_edit(str(m.pk), new_caption)
            print(f"✅ Repaired caption for PK {m.pk} successfully.")
            fixed_count += 1
        except Exception as e:
            print(f"❌ Failed to edit caption for PK {m.pk}: {e}")
            if "feedback_required" in str(e).lower():
                print("🛑 Instagram action block active. Wait 15-30 minutes before retrying.")
                break

        # Rate limit between edits
        edit_delay = getattr(args, "delay", 4)
        time.sleep(edit_delay)

    print("\n--- Caption Repair Summary ---")
    print(f"Repaired: {fixed_count} | Skipped/Unmatched: {skipped_count}\n")


def parse_selection(input_str: str, max_val: int) -> List[int]:
    """Parse selection string like '1,3,5-8' into 1-based indices."""
    input_str = input_str.strip().lower()
    if not input_str or input_str in ("all", "a", "*"):
        return list(range(1, max_val + 1))
    if input_str in ("q", "quit", "exit", "n", "no"):
        return []

    selected = set()
    parts = input_str.split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            sub = part.split("-")
            if len(sub) == 2 and sub[0].strip().isdigit() and sub[1].strip().isdigit():
                start = int(sub[0].strip())
                end = int(sub[1].strip())
                for i in range(min(start, end), max(start, end) + 1):
                    if 1 <= i <= max_val:
                        selected.add(i)
        elif part.isdigit():
            val = int(part)
            if 1 <= val <= max_val:
                selected.add(val)

    return sorted(list(selected))


def cmd_sync(args: argparse.Namespace) -> None:
    """Compare actual Instagram posts against X reposts and select which ones to upload."""
    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)

    print("\n--- Syncing Missing Posts (Instagram vs X Reposts) ---")
    ig_limit = getattr(args, "limit", 50)
    tw_limit = getattr(args, "twitter_limit", 50)
    dry_run = getattr(args, "dry_run", False)
    upload_all = getattr(args, "all", False)
    cli_select = getattr(args, "select", None)

    # First pass: dry-run to discover missing posts
    try:
        summary = asyncio.run(
            engine.sync_missing_posts(
                twitter_limit=tw_limit,
                ig_limit=ig_limit,
                dry_run=True,
            )
        )
    except Exception as e:
        print(f"❌ Sync failed: {e}")
        return

    missing = summary["missing_posts"]

    print("\n--- Sync Scan Results ---")
    print(f"• Instagram posts inspected: {summary['ig_posts_scanned']}")
    print(f"• Unique X tweet IDs verified on Instagram: {summary['ig_tweet_ids_found']}")
    print(f"• X reposts fetched: {summary['twitter_posts_scanned']}")
    print(f"• Missing posts detected: {len(missing)}")

    if not missing:
        print("\n🎉 All X reposts are already uploaded to Instagram. Nothing to sync!\n")
        return

    print("\nMissing Posts:")
    for idx, p in enumerate(missing, 1):
        m_type_summary = f"{len(p.media_items)} media"
        if p.media_items:
            m_types = [m.media_type.value for m in p.media_items]
            m_type_summary = f"{len(p.media_items)} {m_types[0]}"
        txt = (p.text or "").replace("\n", " ")[:50]
        print(f"  [{idx:2d}] ID: {p.id} | @{p.author_username:<15} ({m_type_summary:<8}) | {txt}")

    if dry_run:
        print("\n💡 Dry-run mode: No posts were uploaded.")
        print("   To select and upload, run: python run.py sync")
        print()
        return

    # Determine which items to upload
    selected_indices: List[int] = []

    if cli_select is not None:
        selected_indices = parse_selection(cli_select, len(missing))
    elif upload_all:
        selected_indices = list(range(1, len(missing) + 1))
    else:
        # Batch selection menu
        print("\n" + "=" * 55)
        print("📋 Batch Selection Menu:")
        print("   • 'all' / 'a'      : Upload all missing posts")
        print("   • '1,3,5'          : Upload specific numbers")
        print("   • '1-5'            : Upload range of numbers")
        print("   • '1-3, 5, 8-10'   : Combined ranges and numbers")
        print("   • 'q' / 'quit'     : Cancel / do nothing")
        print("=" * 55)

        try:
            choice = input(f"\nEnter selection to upload [1-{len(missing)} or 'all'] (q to quit): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return

        selected_indices = parse_selection(choice, len(missing))

    if not selected_indices:
        print("\nNo posts selected for upload. Operation cancelled.\n")
        return

    selected_ids = [missing[i - 1].id for i in selected_indices]
    print(f"\n🚀 Proceeding to upload {len(selected_ids)} selected post(s)...")

    # Run upload on selected IDs
    try:
        upload_summary = asyncio.run(
            engine.sync_missing_posts(
                twitter_limit=tw_limit,
                ig_limit=ig_limit,
                dry_run=False,
                selected_ids=selected_ids,
            )
        )
        print(f"\n✅ Uploaded: {upload_summary['uploaded_count']} | Failed: {upload_summary['failed_count']}\n")
    except Exception as e:
        print(f"\n❌ Upload execution failed: {e}\n")


def cmd_plugins(args: argparse.Namespace) -> None:
    """List loaded plugins and their status."""
    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)
    print("\n--- Active Plugins ---")
    for p in engine.plugin_manager.plugins:
        status = "ENABLED" if p.enabled else "DISABLED"
        print(f"• [{p.priority:02d}] {p.name:<15} ({status}): {p.description}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Instagram Auto Uploader with Anime Face-Preserving Cropping"
    )
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # start
    start_parser = subparsers.add_parser("start", help="Start continuous monitoring daemon")
    start_parser.add_argument(
        "--upload-all",
        action="store_true",
        help="Upload existing posts without skipping baseline",
    )

    # watch
    subparsers.add_parser("watch", help="Watch incoming/ folder for drag-and-drop uploads")

    # check
    check_parser = subparsers.add_parser("check", help="Run a single check and upload cycle")
    check_parser.add_argument(
        "--upload-all",
        action="store_true",
        help="Upload existing posts without skipping baseline",
    )

    # test
    subparsers.add_parser("test", help="Test Instagram and source connections")

    # crop
    crop_parser = subparsers.add_parser("crop", help="Test anime face detection and crop on an image")
    crop_parser.add_argument("--image", "-i", required=True, help="Input image path")
    crop_parser.add_argument("--output", "-o", help="Output cropped image path")
    crop_parser.add_argument(
        "--ratio",
        "-r",
        default="auto",
        choices=["auto", "4:5", "1:1", "1.91:1"],
        help="Target aspect ratio mode",
    )
    crop_parser.add_argument(
        "--model",
        default="yolov3",
        choices=["yolov3", "faster-rcnn"],
        help="Model type for anime-face-detector",
    )
    crop_parser.add_argument(
        "--device",
        default="cpu",
        help="Device to run inference on (cpu or cuda)",
    )
    crop_parser.add_argument(
        "--fallback",
        default="center",
        choices=["center", "smart_pad"],
        help="Fallback behavior when no face is found",
    )

    # stats
    subparsers.add_parser("stats", help="Display service metrics")

    # reset
    reset_parser = subparsers.add_parser("reset", help="Reset seen posts database and clear cached session")
    reset_parser.add_argument(
        "--restore-files",
        "-r",
        action="store_true",
        help="Move previously uploaded files from data/processed/ back to incoming/ folder",
    )

    # plugins
    subparsers.add_parser("plugins", help="List registered plugins")

    # fix-captions
    fix_parser = subparsers.add_parser(
        "fix-captions",
        help="Retroactively repair missing/stripped captions on recent Instagram posts",
    )
    fix_parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max recent Instagram posts to inspect (default: 30)",
    )
    fix_parser.add_argument(
        "--delay",
        type=int,
        default=4,
        help="Seconds to wait between edits to avoid rate limiting (default: 4)",
    )
    fix_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-apply captions even if post already has one",
    )

    # sync / upload-missing
    sync_parser = subparsers.add_parser(
        "sync",
        aliases=["upload-missing"],
        help="Compare actual posts on Instagram with X reposts and upload missing ones",
    )
    sync_parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=50,
        help="Max recent Instagram posts to inspect (default: 50)",
    )
    sync_parser.add_argument(
        "--twitter-limit",
        "-t",
        type=int,
        default=50,
        help="Max X reposts to fetch (default: 50)",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and list missing posts without uploading them",
    )
    sync_parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Upload all missing posts without interactive prompt",
    )
    sync_parser.add_argument(
        "--select",
        "-s",
        type=str,
        default=None,
        help="Specific indices or ranges to upload (e.g. '1,3,5' or '1-5')",
    )
    sync_parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Always show interactive batch selection menu",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "start": cmd_start,
        "watch": cmd_watch,
        "check": cmd_check,
        "test": cmd_test,
        "crop": cmd_crop,
        "stats": cmd_stats,
        "reset": cmd_reset,
        "plugins": cmd_plugins,
        "fix-captions": cmd_fix_captions,
        "sync": cmd_sync,
        "upload-missing": cmd_sync,
    }

    cmd_fn = commands.get(args.command)
    if cmd_fn:
        cmd_fn(args)


if __name__ == "__main__":
    main()
