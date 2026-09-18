"""ProxGram growth subsystem.

Independent, Userbot-based (Telethon) worker that monitors public channels
and posts non-spam comments on new channel posts to drive traffic to the
main proxy channel. Designed to run as a separate process from the main
posting bot so the main bot token is never exposed to restriction risk.
"""

__version__ = "0.1.0"
