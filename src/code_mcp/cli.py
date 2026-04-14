"""CLI command for indexing source code repositories."""

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def index_repos(
    repos_dir: Path | None = None,
    repo_filter: str | None = None,
    rebuild: bool = False,
    verbose: bool = False,
    limit: int | None = None,
    skip_vectors: bool = False,
    background_vectors: bool = False,
    compact: bool = False,
) -> int:
    """Index source code repositories.

    Args:
        repos_dir: Directory containing git repos (default from settings)
        repo_filter: Only index this specific repo name
        rebuild: Force full rebuild (ignore change detection)
        verbose: Enable debug logging
        limit: Max files per repo (for testing)
        skip_vectors: Skip vector embedding (FTS-only, much faster)
        background_vectors: Run vector embedding in background thread
        compact: Compact vector index after indexing (defragments LanceDB)

    Returns:
        Exit code (0 success, 1 error)
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from .code_manager import CodeIndexManager
    from .config import settings

    base_dir = repos_dir or settings.repos_dir
    base_dir = base_dir.expanduser().resolve()

    if not base_dir.is_dir():
        logger.error(f"Source codes directory not found: {base_dir}")
        return 1

    manager = CodeIndexManager()

    # Discover repos (directories)
    repos: list[Path] = []
    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        if repo_filter and entry.name != repo_filter:
            continue
        repos.append(entry)

    if not repos:
        logger.error(f"No repositories found in {base_dir}")
        if repo_filter:
            logger.error(f"  (filtered by: {repo_filter})")
        return 1

    logger.info(f"Found {len(repos)} repositories in {base_dir}")

    total_stats = {"repos": 0, "files": 0, "symbols": 0, "failed": 0}

    for repo_path in repos:
        logger.info("")
        logger.info(f"{'=' * 60}")
        logger.info(f"Repository: {repo_path.name}")
        logger.info(f"{'=' * 60}")

        try:
            stats = manager.index_repo(
                repo_path,
                rebuild=rebuild,
                limit=limit,
                skip_vectors=skip_vectors,
                background_vectors=background_vectors,
            )
            total_stats["repos"] += 1
            total_stats["files"] += stats["indexed"]
            total_stats["symbols"] += stats["symbols"]
            total_stats["failed"] += stats["failed"]
        except Exception as e:
            logger.exception(f"Failed to index {repo_path.name}: {e}")
            total_stats["failed"] += 1

    # Summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("INDEXING COMPLETE (Phase 1)")
    logger.info(f"  Repositories: {total_stats['repos']}")
    logger.info(f"  Files indexed: {total_stats['files']}")
    logger.info(f"  Symbols extracted: {total_stats['symbols']}")
    logger.info(f"  Failed: {total_stats['failed']}")

    # Wait for background embedding threads to finish
    embed_thread = getattr(manager, "_embed_thread", None)
    if embed_thread and embed_thread.is_alive():
        logger.info("")
        logger.info("Waiting for background vector embedding to complete...")
        embed_thread.join()

    idx_stats = manager.get_stats()
    logger.info("")
    logger.info("Index statistics:")
    logger.info(f"  Total repos:   {idx_stats['repo_count']}")
    logger.info(f"  Total files:   {idx_stats['file_count']}")
    logger.info(f"  Total symbols: {idx_stats['symbol_count']}")
    logger.info(f"  FTS size:      {idx_stats['index_size_mb']:.1f} MB")
    logger.info(f"  Vector size:   {idx_stats['vector_index_size_mb']:.1f} MB")
    logger.info(f"  Total size:    {idx_stats['total_index_size_mb']:.1f} MB")
    logger.info("=" * 60)

    if compact and not skip_vectors:
        try:
            manager.embedder.compact()
        except Exception as e:
            logger.warning(f"Vector compaction failed: {e}")

    manager.fts_index.close()
    return 0 if total_stats["failed"] == 0 else 1


def cli_main() -> None:
    """Entry point for code-mcp-index command."""
    parser = argparse.ArgumentParser(
        description="Index source code repositories for Code MCP search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index all repos in ~/source-codes
  code-mcp-index

  # Index a specific repo
  code-mcp-index --repo linux

  # Custom directory with full rebuild
  code-mcp-index --dir ~/projects --rebuild

  # Fast indexing (keyword search only, no embeddings)
  code-mcp-index --skip-vectors

  # Non-blocking: FTS available immediately, vectors build in background
  code-mcp-index --background-vectors

  # Test with limited files
  code-mcp-index --repo cpython --limit 100 --verbose
        """,
    )
    parser.add_argument(
        "--dir",
        type=Path,
        metavar="PATH",
        help="Directory containing repos (default: ~/source-codes)",
    )
    parser.add_argument(
        "--repo",
        metavar="NAME",
        help="Only index this specific repository",
    )
    parser.add_argument("--rebuild", action="store_true", help="Force full rebuild")
    parser.add_argument(
        "--skip-vectors",
        action="store_true",
        help="Skip vector embeddings (FTS keyword search only, much faster)",
    )
    parser.add_argument(
        "--background-vectors",
        action="store_true",
        help="Run vector embedding in background (FTS available immediately)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Compact vector index after indexing (defragments LanceDB)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--limit", type=int, metavar="N", help="Max files per repo (for testing)")

    args = parser.parse_args()
    sys.exit(
        index_repos(
            repos_dir=args.dir,
            repo_filter=args.repo,
            rebuild=args.rebuild,
            verbose=args.verbose,
            limit=args.limit,
            skip_vectors=args.skip_vectors,
            background_vectors=args.background_vectors,
            compact=args.compact,
        )
    )
