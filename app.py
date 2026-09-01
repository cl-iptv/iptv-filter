import os
import time
import threading
import requests
import concurrent.futures
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
    "LAT",
    "24/7",
    "US",
    "ES",
    "4K",
    "MU",
    "WORLD LIVE SPORTS",
    "FORMULA 1",
    "BR",
    "UK",
]

# Prefijos de categorías para PELÍCULAS (VOD).
GRUPOS_VOD_PERMITIDOS = [
    "|EN|",
    "|ES|",
    "4K",
    "Netflix",
    "APPLE",
    "Disney",
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
    "|EN|",
    "|LA|",
    "|MULTI|",
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

# Cuántas categorías de VOD se piden en paralelo al proveedor real.
# Más alto = más rápido, pero más carga simultánea sobre ese proveedor.
# 4 es un buen balance entre velocidad y no verse como abuso.
VOD_PARALELISMO = 4

# ============================================
# NO NECESITAS TOCAR NADA DE AQUÍ PARA ABAJO
# ============================================

# Reutilizar conexiones HTTP (evita re-negociar TLS en cada pedido al
# proveedor real, lo cual acelera las peticiones repetidas).
SESSION = requests.Session()

# Hora en la que arrancó este proceso, para mostrar "tiempo activo" en /status.
HORA_INICIO = time.time()

CACHE = {}
# Categorías: livianas (unos pocos KB), se pueden cachear más tiempo.
CACHE_TTL_CATEGORIAS = 900  # 15 minutos
# Catálogos completos (streams): pesados (varios MB), se cachean menos
# tiempo para no acumular mucha memoria por mucho rato.
CACHE_TTL_STREAMS = 120  # 2 minutos

# Campos que la app no necesita para listar/reproducir contenido.
# Quitarlos reduce el tamaño en memoria y lo que se envía a la app.
CAMPOS_INNECESARIOS = {"trailer", "tmdb", "rating_5based", "is_adult", "custom_sid"}


def limpiar_items(items):
    return [
        {k: v for k, v in it.items() if k not in CAMPOS_INNECESARIOS}
        for it in items
    ]


def get_cached_or_fetch(cache_key, url, params, ttl):
    now = time.time()
    if cache_key in CACHE and now - CACHE[cache_key]["ts"] < ttl:
        return CACHE[cache_key]["data"]
    r = SESSION.get(url, params=params, timeout=30)
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


def fetch_directo(action="", category_id=None, series_id=None, vod_id=None, ttl=CACHE_TTL_STREAMS):
    """Pide datos al proveedor real. No depende del contexto de una petición
    Flask, así que se puede usar también desde hilos paralelos."""
    params = {"username": REAL_USER, "password": REAL_PASS}
    if action:
        params["action"] = action
    if category_id:
        params["category_id"] = category_id
    if series_id:
        params["series_id"] = series_id
    if vod_id:
        params["vod_id"] = vod_id

    cache_key = f"{action}:{category_id or ''}:{series_id or ''}:{vod_id or ''}"
    return get_cached_or_fetch(cache_key, f"{REAL_SERVER}/player_api.php", params, ttl)


def player_api(action="", category_id_override=None):
    """Wrapper que lee los parámetros de la petición actual del cliente."""
    category_id = category_id_override or request.values.get("category_id")
    series_id = request.values.get("series_id")
    vod_id = request.values.get("vod_id")
    es_catalogo_grande = action in ("get_live_streams", "get_series", "get_vod_streams")
    ttl = CACHE_TTL_STREAMS if es_catalogo_grande else CACHE_TTL_CATEGORIAS
    return fetch_directo(action, category_id, series_id, vod_id, ttl)


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
            return jsonify(limpiar_items(ordenados))

        if action == "get_vod_streams":
            cats = player_api("get_vod_categories")
            permitidas = filtrar_categorias(cats, GRUPOS_VOD_PERMITIDOS)

            if request.values.get("category_id"):
                # El cliente pidió una categoría específica: comportamiento normal
                streams = player_api(action)
                ids_ok = {c["category_id"] for c in permitidas}
                return jsonify(limpiar_items(filtrar_items(streams, ids_ok, "name")))

            # El cliente pidió TODO el catálogo sin categoría. Este proveedor
            # no devuelve nada si no se especifica category_id, así que
            # pedimos cada categoría permitida por separado y las juntamos.
            # Lo hacemos EN PARALELO (varios hilos a la vez) para que sea
            # mucho más rápido que pedirlas una por una.
            resultado = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=VOD_PARALELISMO) as executor:
                futuros = [
                    executor.submit(fetch_directo, "get_vod_streams", cat["category_id"])
                    for cat in permitidas
                ]
                for futuro in concurrent.futures.as_completed(futuros):
                    try:
                        resultado.extend(futuro.result())
                    except Exception:
                        continue

            return jsonify(limpiar_items(resultado))

        if action == "get_series":
            cats = player_api("get_series_categories")
            ids_ok = ids_categorias_permitidas(cats, GRUPOS_SERIES_PERMITIDOS)
            series = player_api(action)
            return jsonify(limpiar_items(filtrar_items(series, ids_ok, "name")))

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
    r = SESSION.get(
        f"{REAL_SERVER}/xmltv.php",
        params={"username": REAL_USER, "password": REAL_PASS},
        timeout=30,
    )
    return Response(r.content, mimetype=r.headers.get("Content-Type", "application/xml"))


# ---------- PÁGINA DE ESTADO (para ver cómo va el servidor y reiniciarlo) ----------

def formatear_duracion(segundos: float) -> str:
    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas}h {minutos}m {segs}s"
    if minutos:
        return f"{minutos}m {segs}s"
    return f"{segs}s"


def requiere_auth_basica(vista):
    """Decorador: exige usuario/contraseña por HTTP Basic Auth (el cuadro
    de login nativo del navegador), en vez de pasarlos por la URL."""
    from functools import wraps

    @wraps(vista)
    def envoltura(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != LOCAL_USER or auth.password != LOCAL_PASS:
            return Response(
                "Necesitas iniciar sesión para ver esta página.",
                401,
                {"WWW-Authenticate": 'Basic realm="Estado del proxy IPTV"'},
            )
        return vista(*args, **kwargs)

    return envoltura


def pagina_estado():
    ahora = time.time()
    filas_cache = ""
    for key, entry in sorted(CACHE.items(), key=lambda kv: -kv[1]["ts"]):
        edad = ahora - entry["ts"]
        tamano = len(entry["data"]) if isinstance(entry["data"], list) else 1
        filas_cache += f"<tr><td>{key}</td><td>{tamano} elementos</td><td>hace {formatear_duracion(edad)}</td></tr>"

    if not filas_cache:
        filas_cache = "<tr><td colspan='3'>Todavía no hay nada en caché.</td></tr>"

    return f"""
    <html><head><title>Estado del proxy</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: sans-serif; max-width: 700px; margin: 20px auto; padding: 0 16px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #ddd; font-size: 13px; word-break: break-all; }}
        .ok {{ color: #27ae60; font-weight: bold; }}
        .botones {{ display: flex; gap: 10px; margin: 16px 0; }}
        .botones form {{ flex: 1; }}
        button {{ width: 100%; padding: 14px; cursor: pointer; font-size: 15px; border: none; border-radius: 6px; color: white; }}
        .btn-reiniciar {{ background: #c0392b; }}
        .btn-limpiar {{ background: #2980b9; }}
    </style></head>
    <body>
        <h2>Estado del proxy IPTV</h2>
        <p class="ok">● Servidor activo</p>
        <p><b>Tiempo activo:</b> {formatear_duracion(ahora - HORA_INICIO)}</p>
        <p><b>Elementos en caché:</b> {len(CACHE)}</p>

        <div class="botones">
            <form method="post" action="/status/limpiar-cache">
                <button type="submit" class="btn-limpiar">Limpiar caché</button>
            </form>
            <form method="post" action="/status/reiniciar"
                  onsubmit="return confirm('¿Reiniciar el servidor? La app de TV puede tardar un momento en volver a responder.');">
                <button type="submit" class="btn-reiniciar">Reiniciar servidor</button>
            </form>
        </div>

        <h3>Detalle de caché</h3>
        <table>
            <tr><th>Consulta</th><th>Tamaño</th><th>Edad</th></tr>
            {filas_cache}
        </table>
    </body></html>
    """


@app.route("/status", methods=["GET"])
@requiere_auth_basica
def status_route():
    return pagina_estado()


@app.route("/status/limpiar-cache", methods=["POST"])
@requiere_auth_basica
def status_limpiar_cache():
    CACHE.clear()
    return redirect("/status")


@app.route("/status/reiniciar", methods=["POST"])
@requiere_auth_basica
def status_reiniciar():
    def reiniciar_en_un_momento():
        time.sleep(1)  # da tiempo a que la respuesta HTTP llegue al navegador
        os._exit(1)  # gunicorn detecta que el worker murió y levanta uno nuevo

    threading.Thread(target=reiniciar_en_un_momento, daemon=True).start()
    return """
    <html><body style="font-family: sans-serif; max-width: 420px; margin: 60px auto; text-align:center;">
        <h3>Reiniciando el servidor...</h3>
        <p>Espera unos 15-20 segundos y vuelve a entrar a /status para confirmar.</p>
    </body></html>
    """


@app.route("/")
def home():
    if request.values.get("username"):
        return player_api_route()
    return "Proxy Xtream Codes filtrado activo."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
