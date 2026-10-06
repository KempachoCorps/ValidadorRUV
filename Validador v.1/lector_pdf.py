"""Lectura de PDF en lote: en paralelo (varios procesos) y con caché en memoria y en disco.

No depende de Streamlit. Lo usa modulos.py para que el módulo de Llenado no vuelva a leer un PDF que ya leyó,
ni siquiera después de reiniciar la app o de mover el archivo de carpeta.

Tres lectores (el mismo texto que antes, solo cambia cómo y cuándo se lee):
  "pypdf"  texto interno con pypdf (primeras 30 páginas)
  "fitz"   texto interno con PyMuPDF, también ordenado por renglón ('' si PyMuPDF no está instalado)
  "ocr"    OCR de las 2 primeras páginas (PDF escaneados; '' si no hay motor de OCR)

Variables de entorno opcionales:
  PDF_WORKERS   procesos para leer texto (def.: núcleos-1, máx. 6; 1 = sin paralelo)
  OCR_WORKERS   procesos para OCR (def.: núcleos/2, máx. 3; cada uno carga su propio motor y gasta RAM)
  PDF_CACHE=0   no guardar la caché en disco
  PDF_CACHE_DB  ruta del archivo de caché (def.: .cache_lectura_pdf.sqlite3 junto a este archivo)
"""
import atexit, os, sqlite3, threading, zlib
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

# Súbele el número a un lector cuando cambie su lógica: invalida lo guardado en disco.
_VERSION = {"pypdf": "1", "fitz": "1", "ocr": "2"}
_DB = os.environ.get("PDF_CACHE_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     ".cache_lectura_pdf.sqlite3")
_DISCO = os.environ.get("PDF_CACHE", "1") != "0"
_MEM = {}  # (tipo, clave) -> texto
_lock = threading.Lock()


# ---------------- Lectores (corren dentro de los procesos hijos) ----------------
def _leer_pypdf(ruta):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(ruta).pages[:30])


def _leer_fitz(ruta):
    try:
        import fitz
    except ImportError:
        return ""
    partes = []
    with fitz.open(ruta) as d:
        for pg in list(d)[:30]:
            partes.append(pg.get_text())
            w = sorted(pg.get_text("words"), key=lambda t: ((t[1] + t[3]) / 2, t[0]))
            filas = []
            for t in w:
                yc = (t[1] + t[3]) / 2
                if filas and abs(yc - filas[-1][0]) <= 6:
                    filas[-1][1].append(t)
                else:
                    filas.append([yc, [t]])
            partes.append("\n".join(" ".join(x[4] for x in sorted(f, key=lambda t: t[0])) for _, f in filas))
    return "\n".join(partes)


def _leer_ocr(ruta):
    import ocr_pdf
    return ocr_pdf.texto_ocr_pdf(ruta, max_pag=2, parar_si=ocr_pdf.dictamen_completo)


_LECTORES = {"pypdf": _leer_pypdf, "fitz": _leer_fitz, "ocr": _leer_ocr}


def _trabajo(tipo, ruta):
    """Función de los procesos hijos: nunca lanza; devuelve (ruta, texto | None, nombre_del_error | None)."""
    try:
        return ruta, _LECTORES[tipo](ruta), None
    except Exception as e:
        return ruta, None, type(e).__name__


def _iniciar_ocr():
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # Tesseract: un hilo por proceso (el paralelismo ya lo da el pool)


# ---------------- Caché ----------------
def _clave(ruta):
    """Identifica el contenido sin depender de la carpeta: sobrevive a mover el archivo."""
    try:
        s = os.stat(ruta)
    except OSError:
        return None
    return f"{os.path.basename(ruta)}|{s.st_size}|{s.st_mtime_ns}"


def _tipo_db(tipo):
    if tipo == "ocr":
        import ocr_pdf
        return f"ocr{_VERSION['ocr']}-{ocr_pdf.descripcion_motor()}-{ocr_pdf.DPI}-{'r' if ocr_pdf.RAPIDO else 'c'}"
    return tipo + _VERSION[tipo]


def _con():
    con = sqlite3.connect(_DB, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS lectura (clave TEXT, tipo TEXT, texto BLOB, PRIMARY KEY (clave, tipo))")
    return con


def _del_disco(tdb, claves):
    if not _DISCO or not claves:
        return {}
    out = {}
    try:
        with _lock:
            con = _con()
            try:
                for i in range(0, len(claves), 500):
                    ks = claves[i:i + 500]
                    q = f"SELECT clave, texto FROM lectura WHERE tipo=? AND clave IN ({','.join('?' * len(ks))})"
                    for k, blob in con.execute(q, [tdb, *ks]):
                        out[k] = zlib.decompress(blob).decode("utf-8")
            finally:
                con.close()
    except Exception:
        return {}
    return out


def _al_disco(tdb, pares):
    if not _DISCO or not pares:
        return
    try:
        with _lock:
            con = _con()
            try:
                con.executemany("INSERT OR REPLACE INTO lectura VALUES (?,?,?)",
                                [(k, tdb, zlib.compress(t.encode("utf-8", "replace"))) for k, t in pares])
                con.commit()
            finally:
                con.close()
    except Exception:
        pass


# ---------------- Procesos ----------------
_pools = {}


def _pool(n, ocr):
    k = (n, ocr)
    if k not in _pools:
        _pools[k] = ProcessPoolExecutor(max_workers=n, initializer=_iniciar_ocr if ocr else None)
    return _pools[k]


def _cerrar_pools():
    for p in list(_pools.values()):
        p.shutdown(wait=False, cancel_futures=True)
    _pools.clear()


atexit.register(_cerrar_pools)


def _n_procesos(tipo, tareas):
    cpu = os.cpu_count() or 2
    try:
        if tipo == "ocr":
            n, minimo = int(os.environ.get("OCR_WORKERS") or max(1, min(cpu // 2, 3))), 2
        else:
            n, minimo = int(os.environ.get("PDF_WORKERS") or max(1, min(cpu - 1, 6))), 6
    except ValueError:
        n, minimo = 1, 1
    return 1 if tareas < minimo else max(1, min(n, tareas))


# ---------------- API ----------------
def texto(tipo, ruta):
    """Texto de un PDF: memoria -> disco -> lectura directa. Si el lector falla, lanza su misma excepción."""
    k = _clave(ruta)
    if k is not None:
        v = _MEM.get((tipo, k))
        if v is not None:
            return v
        v = _del_disco(_tipo_db(tipo), [k]).get(k)
        if v is not None:
            _MEM[(tipo, k)] = v
            return v
    t = _LECTORES[tipo](ruta)
    if k is not None:
        _MEM[(tipo, k)] = t
        _al_disco(_tipo_db(tipo), [(k, t)])
    return t


def precargar(tipo, rutas, progreso=None):
    """Deja en caché el texto de todas las rutas, leyendo en paralelo lo que falte.
    progreso(hechos, total) se llama mientras avanza. Los archivos que fallan se omiten aquí
    (texto() vuelve a intentarlos y lanza el error real cuando alguien los pida)."""
    total = len(rutas)
    pend = []
    for r in rutas:
        k = _clave(r)
        if k is not None and (tipo, k) not in _MEM:
            pend.append((r, k))
    tdb = _tipo_db(tipo) if pend else ""
    if pend:
        hit = _del_disco(tdb, [k for _, k in pend])
        for k, v in hit.items():
            _MEM[(tipo, k)] = v
        pend = [(r, k) for r, k in pend if k not in hit]
    hechos = total - len(pend)
    if progreso:
        progreso(hechos, total)
    if not pend:
        return

    clave_de = dict(pend)  # ruta -> clave
    nuevos, bloque = [], (1 if tipo == "ocr" else 25)

    def guardar(ruta, txt):
        nonlocal hechos
        hechos += 1
        if txt is not None:
            _MEM[(tipo, clave_de[ruta])] = txt
            nuevos.append((clave_de[ruta], txt))
        if len(nuevos) >= bloque:
            _al_disco(tdb, nuevos[:])
            nuevos.clear()
        if progreso:
            progreso(hechos, total)

    restantes = [r for r, _ in pend]
    try:
        n = _n_procesos(tipo, len(restantes))
        if n > 1:
            futs = []
            try:
                pool = _pool(n, tipo == "ocr")
                futs = [pool.submit(_trabajo, tipo, r) for r in restantes]
                for f in futs:
                    ruta, txt, _err = f.result()
                    guardar(ruta, txt)
                    restantes.remove(ruta)
            except BrokenProcessPool:
                _pools.clear()  # un proceso murió: lo que falte se lee en serie abajo
            finally:
                for f in futs:
                    f.cancel()  # si Streamlit interrumpe la corrida, no deja trabajo en cola
        for r in list(restantes):
            guardar(*_trabajo(tipo, r)[:2])
    finally:
        _al_disco(tdb, nuevos)  # lo ya leído se conserva aunque la corrida se interrumpa
