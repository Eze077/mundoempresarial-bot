# -*- coding: utf-8 -*-
"""Configuración común de las pruebas del bot."""
import os
import tempfile


def pytest_configure(config):
    # Ninguna prueba escribe la configuración real. El 19/11/2026 las pruebas corrieron como root en el VPS con el entorno
    # vacío: cmd_start, sin ADMIN_CHAT_ID, llamó a _persist_admin_chat_id y dejó un ADMIN_CHAT_ID de prueba en
    # /etc/mundoempresarial-bot.env (el harness no pudo mandar avisos hasta que se restauró).
    os.environ["ME_BOT_ENV_FILE"] = os.path.join(tempfile.mkdtemp(prefix="bot-env-"), "mundoempresarial-bot.env")
