#!/usr/bin/env python3
"""CLI entrypoint for Instagram Auto Uploader."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

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

    print("\n--- Statistics ---")
    print(f"Posts checked    : {stats.get('checked', 0)}")
    print(f"Posts published  : {stats.get('published', 0)}")
    print(f"Errors recorded  : {stats.get('errors', 0)}")
    print(f"Baseline ready   : {'Yes' if initialized else 'No'}\n")


def cmd_reset(args: argparse.Namespace) -> None:
    """Reset history and seen posts in state database."""
    cfg = AppConfig.load(args.config)
    engine = AutoUploaderEngine(cfg)
    engine.state.reset_history()
    print("\nState database reset. Next check will establish a new baseline.\n")


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
    subparsers.add_parser("start", help="Start continuous monitoring daemon")

    # watch
    subparsers.add_parser("watch", help="Watch incoming/ folder for drag-and-drop uploads")

    # check
    subparsers.add_parser("check", help="Run a single check and upload cycle")

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
    subparsers.add_parser("reset", help="Reset seen posts database")

    # plugins
    subparsers.add_parser("plugins", help="List registered plugins")

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
    }

    cmd_fn = commands.get(args.command)
    if cmd_fn:
        cmd_fn(args)


if __name__ == "__main__":
    main()
