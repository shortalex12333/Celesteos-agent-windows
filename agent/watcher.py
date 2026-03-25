"""
File system watcher using watchdog.
Detects new/modified/deleted files and adds them to processing queue.
"""

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from pathlib import Path
from typing import Callable, Optional
import threading
import time


class DocumentWatcher(FileSystemEventHandler):
    """
    Watches directories for document changes.

    Features:
        - Monitors PDF, DOCX, XLSX files
        - Debounces rapid changes (e.g., during file copy)
        - Ignores temporary files
        - Calls callback for each file event
    """

    # Ignore patterns — everything else passes through to the daemon's classifier
    IGNORE_PATTERNS = {
        '~$',  # Office temp files
        '.tmp',
        '.temp',
        '.crdownload',  # Chrome downloads
        '.part',  # Partial downloads
        '.DS_Store',
        'desktop.ini',
        'thumbs.db',
        '@eaDir',  # Synology metadata
        '.Spotlight-',
    }

    def __init__(
        self,
        on_file_created: Callable[[Path], None],
        on_file_modified: Callable[[Path], None],
        on_file_deleted: Callable[[Path], None],
        debounce_seconds: float = 2.0
    ):
        """
        Initialize watcher.

        Args:
            on_file_created: Callback for new files
            on_file_modified: Callback for modified files
            on_file_deleted: Callback for deleted files
            debounce_seconds: Wait time before processing (prevents duplicate events)
        """
        super().__init__()
        self.on_file_created = on_file_created
        self.on_file_modified = on_file_modified
        self.on_file_deleted = on_file_deleted
        self.debounce_seconds = debounce_seconds

        # Track recent events for debouncing (guarded by _lock)
        self._recent_events: dict[tuple, float] = {}
        self._last_cleanup = time.time()
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent):
        """Handle file creation event."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        if self._should_process(file_path, 'created'):
            self.on_file_created(file_path)

    def on_modified(self, event: FileSystemEvent):
        """Handle file modification event."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        if self._should_process(file_path, 'modified'):
            self.on_file_modified(file_path)

    def on_deleted(self, event: FileSystemEvent):
        """Handle file deletion event."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        if self._should_process(file_path, 'deleted'):
            self.on_file_deleted(file_path)

    def _should_process(self, file_path: Path, event_type: str) -> bool:
        """
        Check if file should be processed.

        Args:
            file_path: File path
            event_type: Type of event (created/modified/deleted)

        Returns:
            True if should process
        """
        # Check ignore patterns against filename and full path
        filename = file_path.name.lower()
        path_str = str(file_path).lower()
        for pattern in self.IGNORE_PATTERNS:
            if pattern.lower() in filename or pattern.lower() in path_str:
                return False

        # Debounce: check if we recently processed this file
        event_key = (str(file_path), event_type)
        current_time = time.time()

        with self._lock:
            # Cleanup old events periodically
            if current_time - self._last_cleanup > 60:
                self._cleanup_recent_events()

            # Check if this event was recent
            if event_key in self._recent_events:
                return False

            # Record this event with timestamp for cleanup
            self._recent_events[event_key] = current_time

        return True

    def _cleanup_recent_events(self):
        """Remove old events from debounce tracking."""
        current_time = time.time()
        cutoff = current_time - (self.debounce_seconds * 2)

        self._recent_events = {
            key: ts for key, ts in self._recent_events.items()
            if ts > cutoff
        }

        self._last_cleanup = current_time


class FileWatcher:
    """
    File system watcher manager.

    Manages watchdog observer and event handling.
    """

    def __init__(
        self,
        watch_paths: list,
        on_file_created: Callable[[Path], None],
        on_file_modified: Callable[[Path], None],
        on_file_deleted: Callable[[Path], None],
        recursive: bool = True
    ):
        """
        Initialize file watcher.

        Args:
            watch_paths: List of paths to watch
            on_file_created: Callback for new files
            on_file_modified: Callback for modified files
            on_file_deleted: Callback for deleted files
            recursive: Whether to watch subdirectories
        """
        self.watch_paths = watch_paths
        self.recursive = recursive

        # Create event handler
        self.event_handler = DocumentWatcher(
            on_file_created=on_file_created,
            on_file_modified=on_file_modified,
            on_file_deleted=on_file_deleted
        )

        # Create observer
        self.observer = Observer()
        self._watching = False

    def start(self):
        """Start watching file system."""
        if self._watching:
            return

        # Schedule watches
        for watch_path in self.watch_paths:
            path = Path(watch_path)
            if path.exists():
                self.observer.schedule(
                    self.event_handler,
                    str(path),
                    recursive=self.recursive
                )

        self.observer.start()
        self._watching = True

    def stop(self):
        """Stop watching file system."""
        if not self._watching:
            return

        self.observer.stop()
        self.observer.join(timeout=5)
        self._watching = False

    def is_watching(self) -> bool:
        """Check if currently watching."""
        return self._watching


def create_watcher(
    watch_paths: list,
    on_file_created: Callable[[Path], None],
    on_file_modified: Callable[[Path], None],
    on_file_deleted: Callable[[Path], None]
) -> FileWatcher:
    """
    Convenience function to create file watcher.

    Args:
        watch_paths: Paths to watch
        on_file_created: Callback for new files
        on_file_modified: Callback for modified files
        on_file_deleted: Callback for deleted files

    Returns:
        FileWatcher instance
    """
    return FileWatcher(
        watch_paths=watch_paths,
        on_file_created=on_file_created,
        on_file_modified=on_file_modified,
        on_file_deleted=on_file_deleted
    )
