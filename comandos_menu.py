"""Comandos del bot: UNA lista para el menú de Telegram (set_my_commands), el texto de /help y el
informe de uso (informe_uso.py). Antes el /help era un texto escrito a mano que quedó viejo (nombraba
/horarios, /feedback_ver y /cola, que ya no existían, y no nombraba 20 de los 22 del menú).

3/11/2026 (Leo): fuera del menú /stats (solo redirigía a /coladepublicacion), /ciclaje (= /briefing auto),
/vivo e /input_evento (sin uso), /editor (sin uso desde junio) y /fuentes (pasa a un botón de /rutina).
Entra /encuesta (se usaba y no estaba). Los que salen del menú siguen funcionando si se escriben."""

# (grupo, comando, descripción). El orden es el del menú.
MENU = [
    ("Pipeline editorial", "rutina", "Tu rutina diaria + chequeo de salud y fuentes RSS"),
    ("Pipeline editorial", "pipeline", "Estado del pipeline editorial — todas las etapas"),
    ("Pipeline editorial", "programadas", "Notas programadas — reprogramar o publicar ahora"),
    ("Pipeline editorial", "eventos", "Agenda — boletín semanal + efemérides que vienen"),
    ("Pipeline editorial", "briefing", "Briefing manual — /briefing (notas) · /briefing auto (config)"),
    ("Pipeline editorial", "coladepublicacion", "Cola de publicación — confirmar destinos"),
    ("Pipeline editorial", "ingesta", "Disparar ingesta manual de fuentes RSS"),
    ("Pipeline editorial", "nutricion", "Briefing de nutrición — alimentar living notes"),
    ("Contenido", "evento", "Cobertura de evento propio — audios, fotos y texto → nota"),
    ("Contenido", "campania", "Campaña de evento en curso — revivir, armar o cancelar"),
    ("Contenido", "frases", "Crear nota con frase inspiradora + imagen"),
    ("Contenido", "encuesta", "Nueva encuesta — pregunta + opciones a las redes"),
    ("Contenido", "notamanual", "Cargar columna de autor (PDF o texto) y publicarla"),
    ("Contenido", "livingnotes", "Catálogo de notas vivas por categoría"),
    ("Contenido", "reel", "Armar el reel de una ficha viva"),
    ("Contenido", "publicador", "Gestionar nota publicada — links, republicar, borrar"),
    ("Contenido", "editar", "Editar una nota ya publicada"),
]

# Registrados pero fuera del menú: siguen andando si se escriben. Van al /help para no olvidarlos.
OCULTOS = [
    ("fuentes", "Fuentes RSS del EDITOR (también: botón 📡 en /rutina) · /fuentes &lt;dominio&gt; = detalle"),
    ("ciclaje", "= /briefing auto: notas por corrida + horarios del briefing automático"),
    ("editor", "Agenda temática, descubrimientos, living notes"),
    ("vivo", "Cobertura EN VIVO — nota + redes desde el bot (nunca probado con un evento real)"),
    ("input_evento", "Tandas EN VIVO — texto, audios y fotos para la crónica"),
    ("hilo", "/hilo &lt;URL o ID&gt; — hilo de X de una nota publicada"),
    ("borrar", "/borrar &lt;URL o ID&gt; — nota a la papelera de WP"),
    ("inst", "/inst &lt;job_id&gt; &lt;texto&gt; — aprobar una nota con instrucciones"),
    ("lector", "Evaluar una nota con el Lector QA"),
    ("kwtemp", "/kwtemp &lt;keyword&gt; [capa:N] [dias:N] — keyword temporal a la agenda"),
    ("reglas", "Reglas editoriales activas y desactivadas"),
    ("publinotas", "Notas comerciales/publicitarias"),
    ("set_frases_base", "Foto con ese texto = nueva plantilla base de /frases"),
    ("creditos", "Estado de los servicios pagos (OpenAI, X, EnvíaloSimple…)"),
    ("testtwitter", "Diagnóstico de la conexión con X"),
    ("desbloquear", "Desbloquear IPs de redaccion.mundoempresarial.ar"),
    ("stats", "(viejo) redirige a /coladepublicacion"),
    ("STOP", "Pausar el bot (no procesa links ni programados)"),
    ("RESUME", "Reactivar el bot después de /STOP"),
    ("help", "Este listado (también /comandos y /start)"),
]


def texto_ayuda() -> str:
    """/help en HTML, armado de MENU y OCULTOS (no se desactualiza)."""
    out = ["🤖 <b>Bot MundoEmpresarial — comandos</b>", "",
           "📥 <b>Publicar</b>: pegá un link (nota, YouTube, Google News) → scrape + preview + publicación."]
    grupo = None
    for g, cmd, desc in MENU:
        if g != grupo:
            out += ["", f"<b>{g}</b>"]
            grupo = g
        out.append(f"/{cmd} — {desc}")
    out += ["", "<b>Fuera del menú</b> (se escriben a mano)"]
    out += [f"/{cmd} — {desc}" for cmd, desc in OCULTOS]
    return "\n".join(out)
