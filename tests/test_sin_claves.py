# -*- coding: utf-8 -*-
"""Ninguna clave real escrita en el código (Leo, 17/11/2026: «¿cómo protegemos las claves de todos los agentes?»).
Si un agente (Claude, Codex, DeepSeek) escribe un token en un archivo del repo, esta prueba falla y no se despliega.
Las claves viven SOLO en /etc/mundoempresarial-bot.env (VPS) y en claves.txt (fuera de los repos). Nunca imprime el
valor: muestra archivo, línea, tipo y huella sha256[:8]."""
import hashlib
import os
import re
import subprocess

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATRONES = [
    ("token de bot de Telegram", r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),
    ("clave de OpenAI", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}"),
    ("clave de Anthropic", r"\bsk-ant-[A-Za-z0-9_-]{30,}"),
    ("clave de Google", r"\bAIza[0-9A-Za-z_-]{30,}"),
    ("clave de xAI", r"\bxai-[A-Za-z0-9]{30,}"),
    ("token de GitHub", r"\b(?:github_pat_[A-Za-z0-9_]{30,}|ghp_[A-Za-z0-9]{30,})"),
    ("token de Meta", r"\bEAA[A-Za-z0-9]{60,}"),
    ("contraseña de aplicación de WordPress", r"['\"][A-Za-z0-9]{4}( [A-Za-z0-9]{4}){5}['\"]"),
]
# Valores de prueba aceptados: tienen que decirlo en el propio valor.
DE_PRUEBA = re.compile(r"(?i)falsa|fake|prueba|test|example|ejemplo|xxxx|ci-sin-clave|placeholder|0000000|1234567|abcdefg")
EXT = re.compile(r"\.(py|php|js|sh|json|ya?ml|toml|ini|md|html|txt|cfg|conf)$")


def _archivos():
    try:
        out = subprocess.run(["git", "-C", RAIZ, "ls-files", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, timeout=60).stdout
        return [f for f in out.splitlines() if EXT.search(f)]
    except Exception:
        return []


def test_ninguna_clave_real_escrita_en_el_codigo():
    hallazgos = []
    for f in _archivos():
        try:
            texto = open(os.path.join(RAIZ, f), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for n, ln in enumerate(texto.splitlines(), 1):
            for tipo, pat in PATRONES:
                m = re.search(pat, ln)
                if m and not DE_PRUEBA.search(m.group(0)):
                    hallazgos.append("%s:%d · %s · huella %s" % (f, n, tipo, hashlib.sha256(m.group(0).encode()).hexdigest()[:8]))
    assert not hallazgos, ("Hay claves escritas en el código. Sacalas al entorno (/etc/mundoempresarial-bot.env) y "
                           "rotalas si llegaron a GitHub:\n" + "\n".join(hallazgos))
