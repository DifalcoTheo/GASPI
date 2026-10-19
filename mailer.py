# -*- coding: utf-8 -*-
"""
Notificaciones por mail de ARIANA (SMTP de Titan).

Queda DORMIDO hasta que exista datos/notificaciones.json con la casilla
configurada (ariana@) y enabled=true. Mientras tanto, enviar() no hace nada
y no rompe nada: solo devuelve (False, motivo).

Diseño pedido:
  - Automatico, disparado desde GASPI cuando una OF se envia a produccion.
  - Sale de una casilla propia (ariana@), NO de oc@.
  - Reintenta; si el error persiste, deja el mail "en borrador"
    (datos/mail_pendientes.json) y avisa en el log.

La contraseña (app-password de ariana@) va en datos/notificaciones.json, que
NO se comparte ni se sube a ningun repo (igual que secret.key).
"""
import datetime
import json
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

AQUI = Path(__file__).parent
CONFIG = AQUI / "datos" / "notificaciones.json"
PENDIENTES = AQUI / "datos" / "mail_pendientes.json"   # borradores si falla el envio
LOG = AQUI / "datos" / "mail.log"


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}\n")
    except Exception:
        pass


def _config():
    """Devuelve la config si esta completa y habilitada; si no, None (dormido)."""
    if not CONFIG.exists():
        return None
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"config invalida: {e}")
        return None
    faltan = [k for k in ("remitente", "password", "destinatarios") if not c.get(k)]
    if not c.get("enabled") or faltan or not isinstance(c.get("destinatarios"), list):
        return None
    return c


def configurado():
    return _config() is not None


def enviar(asunto, cuerpo_html, destinatarios=None):
    """
    Envia un mail HTML. Devuelve (ok: bool, detalle: str).
    Si las notificaciones no estan configuradas, no envia (y no es error):
    devuelve (False, 'deshabilitado...').
    """
    c = _config()
    if not c:
        return False, "notificaciones deshabilitadas (falta configurar datos/notificaciones.json con ariana@)"

    dests = [d for d in (destinatarios or c["destinatarios"]) if d]
    if not dests:
        return False, "sin destinatarios"

    msg = MIMEText(cuerpo_html, "html", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = formataddr((c.get("nombre_remitente", "ARIANA"), c["remitente"]))
    msg["To"] = ", ".join(dests)

    host = c.get("smtp_host", "smtp.titan.email")
    port = int(c.get("smtp_port", 465))
    ctx = ssl.create_default_context()

    ultimo = None
    for intento in range(1, 4):                       # 3 intentos
        try:
            if port == 465:
                srv = smtplib.SMTP_SSL(host, port, context=ctx, timeout=25)
            else:                                     # 587 u otro: STARTTLS
                srv = smtplib.SMTP(host, port, timeout=25)
                srv.starttls(context=ctx)
            with srv:
                srv.login(c["remitente"], c["password"])
                srv.sendmail(c["remitente"], dests, msg.as_string())
            _log(f"OK enviado '{asunto}' a {dests}")
            return True, "enviado"
        except Exception as e:
            ultimo = str(e)
            _log(f"intento {intento} fallo: {ultimo}")
            time.sleep(2 * intento)

    _guardar_borrador(asunto, cuerpo_html, dests, ultimo)
    _log(f"FALLO definitivo '{asunto}': {ultimo} -> guardado en borradores")
    return False, f"fallo tras 3 intentos: {ultimo} (queda en borrador)"


def _guardar_borrador(asunto, cuerpo, dests, error):
    arr = []
    if PENDIENTES.exists():
        try:
            arr = json.loads(PENDIENTES.read_text(encoding="utf-8"))
        except Exception:
            arr = []
    arr.append({
        "fecha": datetime.datetime.now().isoformat(timespec="seconds"),
        "asunto": asunto, "destinatarios": dests, "cuerpo": cuerpo, "error": error,
    })
    try:
        PENDIENTES.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log(f"no se pudo guardar borrador: {e}")


def reintentar_borradores():
    """Reintenta los mails que habian quedado en borrador. Devuelve (enviados, quedan)."""
    if not PENDIENTES.exists():
        return 0, 0
    try:
        arr = json.loads(PENDIENTES.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0
    quedan, enviados = [], 0
    for m in arr:
        ok, _ = enviar(m["asunto"], m["cuerpo"], m.get("destinatarios"))
        if ok:
            enviados += 1
        else:
            quedan.append(m)
    PENDIENTES.write_text(json.dumps(quedan, ensure_ascii=False, indent=2), encoding="utf-8")
    return enviados, len(quedan)


# =============================================================================
#  Notificaciones concretas
# =============================================================================

def notificar_of_produccion(lote, cliente, lineas, fecha_entrega=None, vendedor=None):
    """Aviso al equipo: una OF se envio a produccion (entro a 'activas')."""
    items = "".join(f"<li>{_esc(x)}</li>" for x in lineas) or "<li>(sin lineas)</li>"
    asunto = f"Nueva OF a produccion: {lote} — {cliente or 'sin cliente'}"
    cuerpo = f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#0f172a">
      <h2 style="margin:0 0 6px">Nueva orden de fabricacion a produccion</h2>
      <p style="font-size:16px;margin:4px 0"><b>{_esc(lote)}</b> &nbsp;·&nbsp; {_esc(cliente or '')}</p>
      <p style="margin:4px 0;color:#475569">Vendedor: {_esc(vendedor or '—')} &nbsp;·&nbsp; Entrega: {_esc(fecha_entrega or '—')}</p>
      <p style="margin:12px 0 4px"><b>Elementos:</b></p>
      <ul>{items}</ul>
      <p style="color:#94a3b8;font-size:12px;margin-top:16px">Enviado automaticamente por ARIANA · GASPI.</p>
    </div>"""
    return enviar(asunto, cuerpo)


def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    # Prueba manual:  python mailer.py
    print("configurado:", configurado())
    ok, det = enviar("Prueba de ARIANA", "<b>Funciona el envio de notificaciones.</b>")
    print(ok, det)
