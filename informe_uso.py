#!/usr/bin/env python3
"""Informe de uso del bot de contenido de ME (pedido de Leo, 3/11/2026): qué comandos, botones y
mensajes se usaron desde que arrancó el registro. Los datos los anota _solo_admin (main.py) en
uso_bot.db; la lista del menú sale de comandos_menu.py. Lo manda el bot de contenido a ADMIN_CHAT_ID.

Cron one-shot /etc/cron.d/informe-uso-bot (3/12 09:30): se borra solo después de mandar.
Uso: informe_uso.py [--dry-run] (imprime sin mandar)."""
import html
import os
import sqlite3
import sys

import requests

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import comandos_menu as cm  # noqa: E402

DB = os.path.join(AQUI, "uso_bot.db")
MAX_TG = 4000


def env():
    out = {}
    for linea in open("/etc/mundoempresarial-bot.env", encoding="utf-8"):
        if "=" in linea and not linea.startswith("#"):
            k, _, v = linea.strip().partition("=")
            out[k] = v
    return out


def dm(ts):
    return "%s/%s" % (ts[8:10], ts[5:7])


def datos():
    if not os.path.exists(DB):   # la crea el bot con la primera interacción del operador
        return (None, None, 0), []
    with sqlite3.connect("file:%s?mode=ro" % DB, uri=True) as c:
        rango = c.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM uso").fetchone()
        filas = c.execute("SELECT tipo, clave, COUNT(*), MAX(ts) FROM uso GROUP BY tipo, clave "
                          "ORDER BY COUNT(*) DESC").fetchall()
    return rango, filas


def armar():
    (desde, hasta, total), filas = datos()
    if not total:
        return "📊 <b>Uso del bot</b>\n\nTodavía no hay nada registrado."
    cmds = {k: (n, u) for t, k, n, u in filas if t == "comando"}
    menu = [c for _g, c, _d in cm.MENU]
    ocultos = [c for c, _d in cm.OCULTOS] + ["comandos", "start"]
    out = ["📊 <b>Uso del bot de contenido</b> — %s a %s (%d interacciones)" % (dm(desde), dm(hasta), total), ""]

    out.append("<b>Comandos del menú</b> (veces · último uso)")
    usados = sorted((c for c in menu if c in cmds), key=lambda c: -cmds[c][0])
    out += ["/%s — %d · %s" % (c, cmds[c][0], dm(cmds[c][1])) for c in usados]
    sin_uso = [c for c in menu if c not in cmds]
    if sin_uso:
        out.append("⚪ <b>Sin uso:</b> " + ", ".join("/" + c for c in sin_uso))

    fuera = sorted((c for c in cmds if c not in menu), key=lambda c: -cmds[c][0])
    if fuera:
        out += ["", "<b>Fuera del menú, usados</b>"]
        out += ["/%s — %d · %s%s" % (html.escape(c), cmds[c][0], dm(cmds[c][1]),
                                     "" if c in ocultos else " (no existe)") for c in fuera]

    botones = [(k, n) for t, k, n, _u in filas if t == "boton"]
    if botones:
        out += ["", "<b>Botones más tocados</b> (familia · veces)"]
        out.append(" · ".join("%s %d" % (html.escape(k), n) for k, n in botones[:15]))

    msgs = [(k, n) for t, k, n, _u in filas if t == "mensaje"]
    if msgs:
        out += ["", "<b>Mensajes:</b> " + " · ".join("%s %d" % (html.escape(k), n) for k, n in msgs)]

    out += ["", "<b>Paso 3 (aprobado el 3/11)</b>: unificar /publicador + /editar + /borrar en /nota, "
                "/rutina como tablero único y /evento como única entrada de eventos. "
                "Con estos números se decide qué entra."]
    txt = "\n".join(out)
    return txt if len(txt) <= MAX_TG else txt[:MAX_TG - 20] + "\n…(recortado)"


def main():
    msg = armar()
    if "--dry-run" in sys.argv:
        print(msg)
        return
    e = env()
    r = requests.post("https://api.telegram.org/bot%s/sendMessage" % e["TELEGRAM_TOKEN"],
                      json={"chat_id": e["ADMIN_CHAT_ID"], "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=25)
    print("send", r.status_code, r.json().get("ok"))
    if not r.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
