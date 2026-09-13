"""Desktop app wrapper for the AVIS assistant."""

from __future__ import annotations

import json
import threading
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from context.avis_context import CONSTANT_PATH, load_constant
from main.model import ConversationMemory, run_assistant
from main.tools import TOOL_FUNCTIONS
from security.security import execute_tool


class AvisApp(QMainWindow):
    """A proper macOS-style desktop client for AVIS."""

    def __init__(self) -> None:
        super().__init__()
        self.memory = ConversationMemory()
        self._setup_window()
        self._setup_ui()
        self._load_context_text()
        self._populate_tools()
        self._show_screen("chat")
        self._append_system("Hello — I can inspect your Mac, run safe tools, and help you act quickly.")

    def _setup_window(self) -> None:
        self.setWindowTitle("AVIS")
        self.resize(1280, 820)
        self.setMinimumSize(980, 620)
        self.setStyleSheet(
            """
            QMainWindow { background: #0b1020; }
            QWidget { color: #e5e7eb; background: #0b1020; }
            QLabel { color: #e5e7eb; }
            QTextEdit, QLineEdit, QListWidget, QPlainTextEdit {
                background: #111827;
                border: 1px solid #1f2937;
                border-radius: 12px;
                color: #e5e7eb;
            }
            QPushButton {
                border: none;
                border-radius: 10px;
                padding: 10px 16px;
                background: #1f2937;
                color: #f9fafb;
                font-weight: 600;
            }
            QPushButton:hover { background: #2b3a4d; }
            QPushButton#primary {
                background: #3b82f6;
                color: white;
            }
            QPushButton#primary:hover { background: #2563eb; }
            QPushButton#success {
                background: #10b981;
                color: #062c22;
            }
            QPushButton#success:hover { background: #059669; }
            QPushButton#ghost {
                background: transparent;
                border: 1px solid #334155;
                color: #e5e7eb;
            }
            QPushButton#ghost:hover { background: #111827; }
            QFrame#sidebar { background: #101827; border: 1px solid #1f2937; border-radius: 18px; }
            QFrame#panel { background: #111827; border: 1px solid #1f2937; border-radius: 18px; }
            QFrame#inner { background: #0f172a; border: 1px solid #1f2937; border-radius: 16px; }
            QListWidget { border-radius: 12px; padding: 6px; }
            QListWidget::item { padding: 8px 10px; border-radius: 8px; }
            QListWidget::item:selected { background: #1d4ed8; color: white; }
            """
        )

    def _setup_ui(self) -> None:
        outer = QWidget(self)
        self.setCentralWidget(outer)

        root = QHBoxLayout(outer)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(18)

        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 18, 16, 14)
        sidebar_layout.setSpacing(10)

        brand = QLabel("AVIS")
        brand.setStyleSheet("font-size: 22px; font-weight: 700; letter-spacing: 0.08em; color: #f9fafb;")
        sidebar_layout.addWidget(brand)

        self.nav_buttons: dict[str, QPushButton] = {}
        for label, name in [("New Chat", "chat"), ("Context", "context"), ("Tools", "tools")]:
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.clicked.connect(lambda checked=False, n=name: self._show_screen(n))
            if name == "chat":
                button.setObjectName("primary")
            self.nav_buttons[name] = button
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch()

        content = QFrame(objectName="panel")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 18)
        content_layout.setSpacing(14)

        self.stack = QStackedWidget()
        self._build_chat_view()
        self._build_context_view()
        self._build_tools_view()
        content_layout.addWidget(self.stack)

        root.addWidget(sidebar)
        root.addWidget(content)

    def _build_chat_view(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("Current chat")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #f9fafb;")
        layout.addWidget(title)

        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setStyleSheet(
            "QTextEdit { background: #0f172a; border: 1px solid #1f2937; border-radius: 16px; padding: 14px; color: #e5e7eb; }"
        )
        self.chat_view.setPlaceholderText("Type a prompt...")
        layout.addWidget(self.chat_view, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(12)
        self.prompt_input = QLineEdit()
        self.prompt_input.setPlaceholderText("Ask AVIS anything...")
        self.prompt_input.returnPressed.connect(self.send_prompt)
        input_row.addWidget(self.prompt_input, 1)

        send_button = QPushButton("Send")
        send_button.setObjectName("primary")
        send_button.clicked.connect(self.send_prompt)
        input_row.addWidget(send_button)

        layout.addLayout(input_row)
        self.stack.addWidget(page)

    def _build_context_view(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("Context")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #f9fafb;")
        layout.addWidget(title)

        self.context_editor = QTextEdit()
        self.context_editor.setStyleSheet(
            "QTextEdit { background: #0f172a; border: 1px solid #1f2937; border-radius: 16px; padding: 14px; color: #e5e7eb; }"
        )
        layout.addWidget(self.context_editor, 1)

        save_button = QPushButton("Save context")
        save_button.setObjectName("success")
        save_button.clicked.connect(self.save_context)
        layout.addWidget(save_button, 0, Qt.AlignmentFlag.AlignRight)

        self.stack.addWidget(page)

    def _build_tools_view(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("Tools")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #f9fafb;")
        layout.addWidget(title)

        self.tools_list = QListWidget()
        self.tools_list.setAlternatingRowColors(False)
        self.tools_list.itemDoubleClicked.connect(lambda item: self.run_named_tool(item.text()))
        layout.addWidget(self.tools_list, 1)

        custom_label = QLabel("Custom tool")
        custom_label.setStyleSheet("color: #94a3b8; font-weight: 600;")
        layout.addWidget(custom_label)

        self.tool_name = QLineEdit()
        self.tool_name.setPlaceholderText("Tool name")
        layout.addWidget(self.tool_name)

        self.tool_args = QTextEdit()
        self.tool_args.setPlaceholderText('{"name": "Spotify"}')
        self.tool_args.setFixedHeight(110)
        self.tool_args.setStyleSheet(
            "QTextEdit { background: #0f172a; border: 1px solid #1f2937; border-radius: 12px; padding: 12px; color: #e5e7eb; }"
        )
        layout.addWidget(self.tool_args)

        run_custom = QPushButton("Run custom tool")
        run_custom.setObjectName("primary")
        run_custom.clicked.connect(self.run_custom_tool)
        layout.addWidget(run_custom)

        self.stack.addWidget(page)

    def _show_screen(self, name: str) -> None:
        for key, button in self.nav_buttons.items():
            if key == name:
                button.setObjectName("primary")
            else:
                button.setObjectName("ghost")
            button.setStyleSheet(button.styleSheet())
        self.stack.setCurrentIndex({"chat": 0, "context": 1, "tools": 2}[name])

    def _populate_tools(self) -> None:
        for tool_name in sorted(TOOL_FUNCTIONS):
            self.tools_list.addItem(QListWidgetItem(tool_name))

    def _append_system(self, text: str) -> None:
        self.chat_view.append(f"<p style='color:#94a3b8; margin:8px 0;'>{text}</p>")

    def _append_user(self, text: str) -> None:
        self.chat_view.append(
            f"<div style='background:#1f2937; border:1px solid #334155; border-radius:12px; padding:10px 12px; margin:8px 0; color:#f8fafc;'>"
            f"{text}</div>"
        )

    def _append_assistant(self, text: str) -> None:
        self.chat_view.append(
            f"<div style='background:#111827; border:1px solid #1f2937; border-radius:12px; padding:10px 12px; margin:8px 0; color:#e5e7eb;'>"
            f"{text}</div>"
        )

    def _append_stream(self, token: str) -> None:
        cursor = self.chat_view.textCursor()
        cursor.movePosition(cursor.End)
        self.chat_view.setTextCursor(cursor)
        self.chat_view.insertPlainText(token)
        self.chat_view.verticalScrollBar().setValue(self.chat_view.verticalScrollBar().maximum())

    def _load_context_text(self) -> None:
        context = load_constant()
        self.context_editor.setPlainText(json.dumps(context, indent=2, ensure_ascii=False))

    def save_context(self) -> None:
        raw = self.context_editor.toPlainText().strip()
        if not raw:
            QMessageBox.warning(self, "Context", "Context cannot be empty.")
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Context must be a JSON object.")
            with open(CONSTANT_PATH, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            QMessageBox.information(self, "Saved", "Constant context saved.")
        except (json.JSONDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid JSON", f"Could not save context:\n{exc}")
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", f"Could not write context:\n{exc}")

    def _request_permission(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        details = ", ".join(f"{key}={value!r}" for key, value in arguments.items()) or "no arguments"
        response = QMessageBox.question(
            self,
            "Permission required",
            f"Allow AVIS to run {tool_name} with {details}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return response == QMessageBox.StandardButton.Yes

    def _do_tool_call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        arguments = arguments or {}
        try:
            output = execute_tool(tool_name, arguments, TOOL_FUNCTIONS, self._request_permission)
        except Exception as exc:
            self._append_assistant(f"Tool error: {exc}")
            return
        self._append_assistant(f"{tool_name}:\n{output}")

    def run_named_tool(self, tool_name: str) -> None:
        self._show_screen("tools")
        self._do_tool_call(tool_name)

    def run_custom_tool(self) -> None:
        tool_name = self.tool_name.text().strip()
        raw = self.tool_args.toPlainText().strip()
        if not tool_name:
            QMessageBox.warning(self, "Missing tool name", "Enter a tool name to run.")
            return
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("Arguments must be a JSON object.")
        except (json.JSONDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid JSON", f"Could not parse arguments:\n{exc}")
            return
        self._show_screen("tools")
        self._do_tool_call(tool_name, payload)

    def send_prompt(self) -> None:
        prompt = self.prompt_input.text().strip()
        if not prompt:
            return
        self.prompt_input.clear()
        self._append_user(prompt)

        def target() -> None:
            try:
                response = run_assistant(
                    prompt,
                    self._request_permission,
                    self.memory,
                    on_token=lambda token: self._append_stream(token),
                )
            except Exception as exc:
                response = f"Error: {exc}"
            self._append_assistant(response)

        threading.Thread(target=target, daemon=True).start()

    def new_chat(self) -> None:
        self.memory = ConversationMemory()
        self.chat_view.clear()
        self._append_system("New chat started.")
        self._show_screen("chat")


if __name__ == "__main__":
    app = QApplication([])
    window = AvisApp()
    window.show()
    app.exec()
