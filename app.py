import os
import time
import requests
from flask import Flask, request, jsonify, redirect, Response

app = Flask(__name__)

# ============================================
# CONFIGURACIÓN - EDITA ESTAS LÍNEAS
# ============================================

# Datos REALES que te dio tu proveedor de IPTV
REAL_SERVER = os.environ.get("REAL_SERVER", "http://tuproveedor.com:8080")
REAL_USER = os.environ.get("REAL_USER", "tu_usuario_real")
REAL_PASS = os.environ.get("REAL_PASS", "tu_password_real")

# Usuario/contraseña que usarás TÚ en la app de TV para entrar a este proxy.
# Estos NO tienen valor por defecto a propósito: deben configurarse como
# variables de entorno en Render, para que no queden visibles en GitHub.
LOCAL_USER = os.environ.get("LOCAL_USER", "cambia_este_usuario")
LOCAL_PASS = os.environ.get("LOCAL_PASS", "cambia_esta_clave")

# Prefijos de categorías para TV EN VIVO. Se incluye cualquier categoría
# cuyo nombre EMPIECE con alguno de estos textos (sin importar mayúsculas).
GRUPOS_LIVE_PERMITIDOS = [
    "World live sports",
    "LAT",
    "US",
    "24/7",
    "MU",
    "Formula 1",
    "4K",
    "ES",
    "BR",
    "UK",
]

# Prefijos de categorías para PELÍCULAS (VOD).
GRUPOS_VOD_PERMITIDOS = [
    "TOP IMDB",
    "|EN|",
    "4K",
    "Netflix",
    "APPLE",
    "Disney",
    "|ES|",
]

# Orden de prioridad para TV en vivo: los canales de estas categorías
# aparecen primero, en este orden. El resto queda después, en el orden
# que ya traía el proveedor.
ORDEN_PRIORIDAD_LIVE = [
    "LAT",
    "US",
    "24/7",
    "ES",
]

# Prefijos de categorías para SERIES.
GRUPOS_SERIES_PERMITIDOS = [
    "|MULTI|",
    "|EN|",
    "|LA|",
    "|ES|",
    "|SCA|",
]

# Nombres (o parte del nombre) de canales/películas/series específicos
# que quieres mantener aunque no estén en los grupos de arriba.
CANALES_PERMITIDOS = [
    "ESPN",
    "TNT Sports",
    "CNN",
]

# ============================================
# NO NECESITAS TOCAR NADA DE AQUÍ PARA ABAJO
# ============================================

CACHE = {}
CACHE_TTL = 300  # segundos


def get_cached_or_fetch(cache_key, url, params):
    now = time.time()
    if cache_key in CACHE and now - CACHE[cache_key]["ts"] < CACHE_TTL:
        return CACHE[cache_key]["data"]
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    CACHE[cache_key] = {"data": data, "ts": now}
    return data


def credenciales_validas(req):
    return (
        req.values.get("username") == LOCAL_USER
        and req.values.get("password") == LOCAL_PASS
    )


def nombre_permitido(nombre: str) -> bool:
    nombre_lower = nombre.lower()
    return any(c.lower() in nombre_lower for c in CANALES_PERMITIDOS)


def categoria_permitida(nombre: str, lista_prefijos) -> bool:
    nombre_lower = nombre.lower().strip()
    return any(nombre_lower.startswith(g.lower().strip()) for g in lista_prefijos)


def player_api(action="", category_id_override=None):
    """Hace la llamada equivalente contra el proveedor real."""
    params = {"username": REAL_USER, "password": REAL_PASS}
    if action:
        params["action"] = action

    category_id = category_id_override or request.values.get("category_id")
    if category_id:
        params["category_id"] = category_id
    if request.values.get("series_id"):
        params["series_id"] = request.values.get("series_id")
    if request.values.get("vod_id"):
        params["vod_id"] = request.values.get("vod_id")

    cache_key = f"{action}:{category_id or ''}:{request.values.get('series_id','')}:{request.values.get('vod_id','')}"
    return get_cached_or_fetch(cache_key, f"{REAL_SERVER}/player_api.php", params)


def filtrar_categorias(categorias, lista_prefijos):
    return [c for c in categorias if categoria_permitida(c.get("category_name", ""), lista_prefijos)]


def ids_categorias_permitidas(categorias, lista_prefijos):
    return {
        str(c["category_id"])
        for c in categorias
        if categoria_permitida(c.get("category_name", ""), lista_prefijos)
    }


def ordenar_por_prioridad(items, cats, lista_prioridad):
    """Reordena items poniendo primero los de las categorías en lista_prioridad,
    en ese orden. El resto mantiene su orden original al final."""
    nombre_por_id = {str(c["category_id"]): c.get("category_name", "") for c in cats}

    def prioridad(item):
        nombre_cat = nombre_por_id.get(str(item.get("category_id")), "")
        nombre_cat_lower = nombre_cat.lower().strip()
        for i, prefijo in enumerate(lista_prioridad):
            if nombre_cat_lower.startswith(prefijo.lower().strip()):
                return i
        return len(lista_prioridad)  # todo lo demás, al final

    return sorted(items, key=prioridad)


def filtrar_items(items, ids_cat_permitidas, name_field):
    resultado = []
    for it in items:
        if str(it.get("category_id")) in ids_cat_permitidas:
            resultado.append(it)
        elif nombre_permitido(it.get(name_field, "")):
            resultado.append(it)
    return resultado


# ---------- LOGIN Y DATOS ----------

@app.route("/player_api.php", methods=["GET", "POST"])
def player_api_route():
    if not credenciales_validas(request):
        return jsonify({"user_info": {"auth": 0}}), 401

    action = request.values.get("action", "")

    try:
        if action == "":
            data = player_api("")
            return jsonify(data)

        if action == "get_live_categories":
            data = player_api(action)
            return jsonify(filtrar_categorias(data, GRUPOS_LIVE_PERMITIDOS))

        if action == "get_vod_categories":
            data = player_api(action)
            return jsonify(filtrar_categorias(data, GRUPOS_VOD_PERMITIDOS))

        if action == "get_series_categories":
            data = player_api(action)
            return jsonify(filtrar_categorias(data, GRUPOS_SERIES_PERMITIDOS))

        if action == "get_live_streams":
            cats = player_api("get_live_categories")
            ids_ok = ids_categorias_permitidas(cats, GRUPOS_LIVE_PERMITIDOS)
            streams = player_api(action)
            filtrados = filtrar_items(streams, ids_ok, "name")
            ordenados = ordenar_por_prioridad(filtrados, cats, ORDEN_PRIORIDAD_LIVE)
            return jsonify(ordenados)

        if action == "get_vod_streams":
            cats = player_api("get_vod_categories")
            permitidas = filtrar_categorias(cats, GRUPOS_VOD_PERMITIDOS)

            if request.values.get("category_id"):
                # El cliente pidió una categoría específica: comportamiento normal
                streams = player_api(action)
                ids_ok = {c["category_id"] for c in permitidas}
                return jsonify(filtrar_items(streams, ids_ok, "name"))

            # El cliente pidió TODO el catálogo sin categoría. Este proveedor
            # no devuelve nada si no se especifica category_id, así que
            # pedimos cada categoría permitida por separado y las juntamos.
            resultado = []
            for cat in permitidas:
                cat_id = cat["category_id"]
                try:
                    streams_cat = player_api(action, category_id_override=cat_id)
                    resultado.extend(streams_cat)
                except Exception:
                    continue

            return jsonify(resultado)

        if action == "get_series":
            cats = player_api("get_series_categories")
            ids_ok = ids_categorias_permitidas(cats, GRUPOS_SERIES_PERMITIDOS)
            series = player_api(action)
            return jsonify(filtrar_items(series, ids_ok, "name"))

        # Cualquier otra acción (get_series_info, get_vod_info, etc.)
        # se pasa tal cual, sin filtrar, para que la app funcione normal
        # al abrir detalles de algo que ya fue filtrado antes.
        data = player_api(action)
        return jsonify(data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- STREAMS (redirige al servidor real) ----------

@app.route("/live/<user>/<pw>/<path:stream_file>")
def live_stream(user, pw, stream_file):
    if user != LOCAL_USER or pw != LOCAL_PASS:
        return "No autorizado", 401
    return redirect(f"{REAL_SERVER}/live/{REAL_USER}/{REAL_PASS}/{stream_file}")


@app.route("/movie/<user>/<pw>/<path:stream_file>")
def movie_stream(user, pw, stream_file):
    if user != LOCAL_USER or pw != LOCAL_PASS:
        return "No autorizado", 401
    return redirect(f"{REAL_SERVER}/movie/{REAL_USER}/{REAL_PASS}/{stream_file}")


@app.route("/series/<user>/<pw>/<path:stream_file>")
def series_stream(user, pw, stream_file):
    if user != LOCAL_USER or pw != LOCAL_PASS:
        return "No autorizado", 401
    return redirect(f"{REAL_SERVER}/series/{REAL_USER}/{REAL_PASS}/{stream_file}")


# Algunos reproductores (ej. Smarters) piden el stream SIN el prefijo /live/,
# directo como /usuario/password/id. A veces además agregan un punto pegado
# a la contraseña cuando no hay extensión de archivo (ej: "prueba1.").
@app.route("/<user>/<pw>/<path:stream_file>")
def short_stream(user, pw, stream_file):
    sufijo = ""
    pw_comparar = pw
    if pw.endswith("."):
        pw_comparar = pw[:-1]
        sufijo = "."
    if user != LOCAL_USER or pw_comparar != LOCAL_PASS:
        return "No autorizado", 401
    return redirect(f"{REAL_SERVER}/{REAL_USER}/{REAL_PASS}{sufijo}/{stream_file}")


# ---------- EPG (opcional, pasa tal cual) ----------

@app.route("/xmltv.php")
def xmltv():
    if not credenciales_validas(request):
        return "No autorizado", 401
    r = requests.get(
        f"{REAL_SERVER}/xmltv.php",
        params={"username": REAL_USER, "password": REAL_PASS},
        timeout=30,
    )
    return Response(r.content, mimetype=r.headers.get("Content-Type", "application/xml"))


@app.route("/")
def home():
    if request.values.get("username"):
        return player_api_route()
    return "Proxy Xtream Codes filtrado activo."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
