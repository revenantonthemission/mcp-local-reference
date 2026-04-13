"""File system watcher for source code repositories.

Monitors indexed repositories for file changes and triggers
incremental reindexing when source or doc files are added, modified, or deleted.
"""

import logging
import time
from pathlib import Path
from threading import Event, Lock, Thread

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .code_manager import DEFAULT_EXCLUDE_PATTERNS, CodeIndexManager
from .config import settings
from .parser import supported_extensions

logger = logging.getLogger(__name__)


class CodeFileHandler(FileSystemEventHandler):
    """Handle file system events in source code repositories."""

    def __init__(
        self,
        manager: CodeIndexManager,
        repo_name: str,
        repo_path: Path,
        debounce_seconds: float = 2.0,
    ):
        super().__init__()
        self.manager = manager
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.debounce_seconds = debounce_seconds
        self._pending_paths: set[Path] = set()
        self._deleted_paths: set[Path] = set()
        self._pending_lock = Lock()
        self._last_event_time = 0.0
        self._extensions = supported_extensions()

    def _should_process(self, path: Path) -> bool:
        """Check if file should be processed."""
        # Only process supported extensions
        if path.suffix.lower() not in self._extensions:
            return False

        # Ignore hidden files and temp files
        if path.name.startswith(".") or path.name.endswith("~"):
            return False

        # Check exclude patterns
        try:
            rel = path.relative_to(self.repo_path)
            for part in rel.parts:
                if part.startswith(".") or part in DEFAULT_EXCLUDE_PATTERNS:
                    return False
        except ValueError:
            return False

        return True

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events."""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_process(path):
            return
        logger.debug(f"File created: {path}")
        with self._pending_lock:
            self._pending_paths.add(path)
            self._last_event_time = time.time()

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events."""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_process(path):
            return
        logger.debug(f"File modified: {path}")
        with self._pending_lock:
            self._pending_paths.add(path)
            self._last_event_time = time.time()

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Handle file deletion events."""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_process(path):
            return
        logger.debug(f"File deleted: {path}")
        with self._pending_lock:
            self._pending_paths.discard(path)
            self._deleted_paths.add(path)
            self._last_event_time = time.time()

    def process_pending(self) -> None:
        """Process pending file changes after debounce period."""
        with self._pending_lock:
            if not self._pending_paths and not self._deleted_paths:
                return

            elapsed = time.time() - self._last_event_time
            if elapsed < self.debounce_seconds:
                return

            paths_to_index = list(self._pending_paths)
            paths_to_delete = list(self._deleted_paths)
            self._pending_paths.clear()
            self._deleted_paths.clear()

        # Process deletions
        for path in paths_to_delete:
            try:
                rel_path = str(path.relative_to(self.repo_path))
                self.manager.remove_file_by_path(self.repo_name, rel_path)
                logger.info(f"Removed from index: {self.repo_name}:{rel_path}")
            except Exception as e:
                logger.error(f"Failed to remove {path}: {e}")

        # Process additions/modifications
        for path in paths_to_index:
            if not path.exists():
                logger.debug(f"Skipping {path}: file no longer exists")
                continue
            try:
                result = self.manager.reindex_file(self.repo_name, self.repo_path, path)
                rel_path = str(path.relative_to(self.repo_path))
                logger.info(f"Reindex {self.repo_name}:{rel_path}: {result}")
            except Exception as e:
                logger.error(f"Failed to reindex {path}: {e}")


class CodeWatcher:
    """Watch indexed source code repositories for changes."""

    def __init__(
        self,
        manager: CodeIndexManager,
        debounce_seconds: float | None = None,
    ):
        self.manager = manager
        self.debounce_seconds = debounce_seconds or settings.watch_debounce_seconds
        self.observer = Observer()
        self._handlers: list[CodeFileHandler] = []
        self._stop_event = Event()
        self._process_thread: Thread | None = None

    def watch_repo(self, repo_name: str, repo_path: Path) -> None:
        """Add a repository to watch."""
        repo_path = repo_path.expanduser().resolve()
        if not repo_path.is_dir():
            logger.warning(f"Cannot watch {repo_name}: {repo_path} not found")
            return

        handler = CodeFileHandler(self.manager, repo_name, repo_path, self.debounce_seconds)
        self.observer.schedule(handler, str(repo_path), recursive=True)
        self._handlers.append(handler)
        logger.info(f"Watching repo: {repo_name} at {repo_path}")

    def watch_all_indexed(self) -> None:
        """Watch all currently indexed repositories."""
        for repo in self.manager.list_repos():
            repo_path = Path(repo["path"])
            self.watch_repo(repo["name"], repo_path)

    def start(self) -> None:
        """Start watching for file changes."""
        if not self._handlers:
            self.watch_all_indexed()

        if not self._handlers:
            logger.info("No repositories to watch")
            return

        logger.info(f"Starting code file watcher ({len(self._handlers)} repos)")
        self.observer.start()
        self._process_thread = Thread(target=self._process_loop, daemon=True)
        self._process_thread.start()

    def stop(self) -> None:
        """Stop watching for file changes."""
        logger.info("Stopping code file watcher")
        self._stop_event.set()

        if self._process_thread:
            self._process_thread.join(timeout=5)

        self.observer.stop()
        self.observer.join()

    def _process_loop(self) -> None:
        """Background loop for processing pending events."""
        while not self._stop_event.is_set():
            for handler in self._handlers:
                try:
                    handler.process_pending()
                except Exception as e:
                    logger.exception(f"Error in process loop: {e}")
            self._stop_event.wait(0.5)
