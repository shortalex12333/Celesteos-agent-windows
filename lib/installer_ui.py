"""
CelesteOS Installation UI
==========================
Simple GUI for installation progress feedback.

Uses Tkinter (built into Python) for cross-platform compatibility.
Shows installation status, progress, and handles user interaction.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional
from enum import Enum
from dataclasses import dataclass
import threading


class InstallUIState(Enum):
    """UI states matching InstallState."""
    INITIALIZING = "Initializing..."
    REGISTERING = "Registering with cloud..."
    WAITING_ACTIVATION = "Waiting for activation..."
    ACTIVATING = "Activating..."
    VERIFYING = "Verifying credentials..."
    COMPLETE = "Installation complete!"
    ERROR = "Installation failed"


@dataclass
class InstallProgress:
    """Progress information."""
    state: InstallUIState
    message: str
    progress: float = 0.0  # 0.0 to 1.0
    details: str = ""
    elapsed_seconds: int = 0


class InstallerWindow:
    """
    Main installation window.

    Displays:
    - Yacht name
    - Current status
    - Progress bar
    - Detailed messages
    - Action buttons
    """

    def __init__(self, yacht_id: str, yacht_name: str, buyer_email: str):
        self.yacht_id = yacht_id
        self.yacht_name = yacht_name
        self.buyer_email = buyer_email

        self.root = tk.Tk()
        self.root.title("CelesteOS Installation")
        self.root.geometry("500x350")
        self.root.resizable(False, False)

        # Center window
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')

        self._setup_ui()
        self._current_state = InstallUIState.INITIALIZING

    def _setup_ui(self):
        """Setup UI components."""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Logo/Title
        title = ttk.Label(
            main_frame,
            text="⚓ CelesteOS",
            font=("Helvetica", 24, "bold")
        )
        title.grid(row=0, column=0, columnspan=2, pady=(0, 10))

        # Yacht info
        yacht_label = ttk.Label(
            main_frame,
            text=f"Installing for: {self.yacht_name}",
            font=("Helvetica", 12)
        )
        yacht_label.grid(row=1, column=0, columnspan=2, pady=(0, 20))

        # Status label
        self.status_label = ttk.Label(
            main_frame,
            text="Initializing...",
            font=("Helvetica", 10, "bold")
        )
        self.status_label.grid(row=2, column=0, columnspan=2, pady=(0, 10))

        # Progress bar
        self.progress_bar = ttk.Progressbar(
            main_frame,
            mode='indeterminate',
            length=400
        )
        self.progress_bar.grid(row=3, column=0, columnspan=2, pady=(0, 10))
        self.progress_bar.start(10)

        # Details text box
        self.details_text = tk.Text(
            main_frame,
            height=8,
            width=55,
            wrap=tk.WORD,
            font=("Monaco", 9),
            bg="#f5f5f5",
            relief=tk.FLAT
        )
        self.details_text.grid(row=4, column=0, columnspan=2, pady=(0, 15))
        self.details_text.config(state=tk.DISABLED)

        # Email info (shown during activation wait)
        self.email_label = ttk.Label(
            main_frame,
            text="",
            font=("Helvetica", 9),
            foreground="blue"
        )
        self.email_label.grid(row=5, column=0, columnspan=2, pady=(0, 10))

        # Action button
        self.action_button = ttk.Button(
            main_frame,
            text="Cancel",
            command=self._on_cancel
        )
        self.action_button.grid(row=6, column=0, columnspan=2)

    def update_progress(self, progress: InstallProgress):
        """Update UI with new progress information."""
        self._current_state = progress.state

        # Update status label
        self.status_label.config(text=progress.message)

        # Update progress bar
        if progress.progress > 0:
            self.progress_bar.stop()
            self.progress_bar.config(mode='determinate')
            self.progress_bar['value'] = progress.progress * 100
        else:
            if self.progress_bar['mode'] != 'indeterminate':
                self.progress_bar.config(mode='indeterminate')
                self.progress_bar.start(10)

        # Update details
        if progress.details:
            self.details_text.config(state=tk.NORMAL)
            self.details_text.insert(tk.END, f"{progress.details}\n")
            self.details_text.see(tk.END)
            self.details_text.config(state=tk.DISABLED)

        # Show email during activation wait
        if progress.state == InstallUIState.WAITING_ACTIVATION:
            if progress.elapsed_seconds > 0:
                mins = progress.elapsed_seconds // 60
                secs = progress.elapsed_seconds % 60
                self.email_label.config(
                    text=f"Check your email: {self.buyer_email} (waiting {mins:02d}:{secs:02d})"
                )
            else:
                self.email_label.config(
                    text=f"Check your email: {self.buyer_email}"
                )
        else:
            self.email_label.config(text="")

        # Update button
        if progress.state == InstallUIState.COMPLETE:
            self.action_button.config(text="Close", command=self._on_close)
            self.progress_bar.stop()
            self.progress_bar['value'] = 100
        elif progress.state == InstallUIState.ERROR:
            self.action_button.config(text="Quit", command=self._on_close)
            self.progress_bar.stop()

    def _on_cancel(self):
        """Handle cancel button."""
        if messagebox.askyesno("Cancel Installation", "Are you sure you want to cancel?"):
            self.root.quit()

    def _on_close(self):
        """Handle close button."""
        self.root.quit()

    def run(self):
        """Start the UI main loop."""
        self.root.mainloop()

    def destroy(self):
        """Close the window."""
        self.root.destroy()


class InstallerUI:
    """
    UI coordinator for installation process.

    Runs UI in main thread, installation in background thread.
    """

    def __init__(self, yacht_id: str, yacht_name: str, buyer_email: str):
        self.yacht_id = yacht_id
        self.yacht_name = yacht_name
        self.buyer_email = buyer_email
        self.window: Optional[InstallerWindow] = None

    def start(self, install_func: Callable):
        """
        Start installation with UI.

        Args:
            install_func: Function to run installation (takes progress_callback)
        """
        # Create window
        self.window = InstallerWindow(
            self.yacht_id,
            self.yacht_name,
            self.buyer_email
        )

        # Start installation in background thread
        install_thread = threading.Thread(
            target=self._run_installation,
            args=(install_func,),
            daemon=True
        )
        install_thread.start()

        # Run UI (blocks)
        self.window.run()

    def _run_installation(self, install_func: Callable):
        """Run installation function with progress callback."""
        def progress_callback(progress: InstallProgress):
            if self.window:
                self.window.root.after(0, self.window.update_progress, progress)

        try:
            install_func(progress_callback)
        except Exception as e:
            # Show error in UI
            progress_callback(InstallProgress(
                state=InstallUIState.ERROR,
                message="Installation failed",
                details=str(e)
            ))


# Example usage
if __name__ == "__main__":
    import time

    def mock_installation(progress_callback):
        """Mock installation for testing."""
        steps = [
            (InstallUIState.INITIALIZING, "Checking system...", 0.1),
            (InstallUIState.REGISTERING, "Registering with cloud...", 0.3),
            (InstallUIState.WAITING_ACTIVATION, "Waiting for email activation...", 0.5),
            (InstallUIState.ACTIVATING, "Activating yacht...", 0.7),
            (InstallUIState.VERIFYING, "Verifying credentials...", 0.9),
            (InstallUIState.COMPLETE, "Installation complete!", 1.0),
        ]

        for state, message, prog in steps:
            time.sleep(2)
            progress_callback(InstallProgress(
                state=state,
                message=message,
                progress=prog,
                details=f"[{time.strftime('%H:%M:%S')}] {message}"
            ))

    ui = InstallerUI("TEST_YACHT_001", "M/Y Test Vessel", "test@example.com")
    ui.start(mock_installation)
