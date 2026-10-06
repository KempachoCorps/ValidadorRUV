"""OCR para PDF escaneados (sin texto interno) y reconocimiento tolerante a errores de lectura del
título 'Dictamen de Obra Terminada' (con o sin 'por Vivienda').

Motores (se usa el primero disponible; con la variable de entorno OCR_MOTOR=tesseract|rapidocr se fuerza uno):
  - RapidOCR:   pip install rapidocr-onnxruntime        (solo pip; el paquete ya trae los modelos)
  - Tesseract:  instalar el programa con el idioma español ('spa') + pip install pytesseract
Si no hay ninguno instalado, la app sigue funcionando como antes (los escaneados se detectan solo por nombre).

Variables de entorno opcionales: OCR_DPI (def. 200), TESSERACT_CMD (ruta a tesseract.exe).
"""
import difflib, functools, io, os, re, shutil, threading, unicodedata
from importlib.util import find_spec

DPI = int(os.environ.get("OCR_DPI", "200"))
MAX_LADO = 4200  # px: páginas enormes (planos, A0) se reducen para no tardar de más
UMBRAL = 0.80  # parecido mínimo (0-1) para dar por bueno el título leído por OCR
# Lectura escalonada (rápida): primero se lee solo el FRAC_SUP superior de la página, donde va el título 'Dictamen de
# Obra Terminada'. Si no está, el documento no es un dictamen y ya no se lee más; si está, se lee el resto de la página.
# OCR_RAPIDO=0 vuelve a leer siempre la página completa (y la 2a página) como antes.
RAPIDO = os.environ.get("OCR_RAPIDO", "1").strip() != "0"
FRAC_SUP = 0.40
SOLAPE = 0.04  # el resto de la página arranca un poco antes para no partir un renglón a la mitad

_lock = threading.Lock()
_listos = {}  # nombre del motor -> función leer(imagen PIL) -> texto (None si no se pudo iniciar)


def _sin_acentos(t):
    return unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode()


# ---------------- Motores de OCR ----------------
def _ruta_tesseract():
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if cmd:
        return cmd
    for c in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
        if os.path.isfile(c):
            return c
    return None


@functools.lru_cache(maxsize=1)
def _tesseract():
    """(ruta, idioma) si Tesseract y pytesseract están utilizables; si no, None."""
    if find_spec("pytesseract") is None:
        return None
    cmd = _ruta_tesseract()
    if not cmd:
        return None
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        langs = set(pytesseract.get_languages(config=""))
    except Exception:
        return None
    lang = "spa" if "spa" in langs else ("eng" if "eng" in langs else None)
    return (cmd, lang) if lang else None


def motores():
    """Motores utilizables, en orden de preferencia (revisión barata: no carga modelos)."""
    t = _tesseract()
    out = []
    if t and t[1] == "spa":
        out.append("tesseract")
    if find_spec("rapidocr_onnxruntime") or find_spec("rapidocr"):
        out.append("rapidocr")
    if t and t[1] != "spa":
        out.append("tesseract")  # sin el idioma español lee peor; queda como último recurso
    pref = os.environ.get("OCR_MOTOR", "").strip().lower()
    if pref in out:
        out.remove(pref)
        out.insert(0, pref)
    return out


def ocr_disponible():
    return bool(motores())


def descripcion_motor():
    m = motores()
    if not m:
        return ""
    if m[0] == "tesseract":
        return f"Tesseract ({_tesseract()[1]})"
    return "RapidOCR"


def _renglones(cajas):
    """[(caja de 4 puntos, texto)] -> texto ordenado por renglón (la etiqueta y su valor quedan en la misma línea)."""
    items = []
    for box, txt in cajas:
        ys, xs = [p[1] for p in box], [p[0] for p in box]
        items.append((sum(ys) / len(ys), min(xs), max(ys) - min(ys), str(txt)))
    if not items:
        return ""
    alt = sorted(i[2] for i in items)
    tol = max(alt[len(alt) // 2] * 0.6, 4)
    filas = []
    for it in sorted(items, key=lambda i: i[0]):
        if filas and abs(it[0] - filas[-1][0]) <= tol:
            filas[-1][1].append(it)
        else:
            filas.append([it[0], [it]])
    return "\n".join(" ".join(i[3] for i in sorted(f, key=lambda i: i[1])) for _, f in filas)


def _crear_tesseract():
    import pytesseract
    cmd, lang = _tesseract()
    pytesseract.pytesseract.tesseract_cmd = cmd
    return lambda img: pytesseract.image_to_string(img, lang=lang, config="--psm 3", timeout=90)


def _crear_rapidocr():
    import numpy as np
    try:
        from rapidocr_onnxruntime import RapidOCR
        v2 = False
    except ImportError:
        from rapidocr import RapidOCR
        v2 = True
    motor = RapidOCR()

    def leer(img):
        arr = np.array(img.convert("RGB"))[:, :, ::-1]
        if v2:
            r = motor(arr)
            cajas = list(zip(r.boxes, r.txts)) if getattr(r, "txts", None) else []
        else:
            res, _ = motor(arr)
            cajas = [(b, t) for b, t, _s in (res or [])]
        return _renglones(cajas)
    return leer


def _motor():
    """(nombre, leer) del primer motor que arranque bien, o None."""
    with _lock:
        for nombre in motores():
            if nombre not in _listos:
                try:
                    _listos[nombre] = _crear_tesseract() if nombre == "tesseract" else _crear_rapidocr()
                except Exception:
                    _listos[nombre] = None
            if _listos[nombre]:
                return nombre, _listos[nombre]
    return None


# ---------------- Lectura de páginas ----------------
def _imagen(pagina):
    import fitz
    from PIL import Image
    r = pagina.rect
    z = min(DPI / 72, MAX_LADO / max(r.width, r.height))
    pix = pagina.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png")))


_COMUNES = {"de", "la", "el", "en", "por", "del", "los", "las", "que", "con", "para", "obra", "terminada",
            "dictamen", "vivienda", "orden", "fecha", "verificacion", "numero", "estado", "cuenta", "factura"}


def _puntaje(t):
    """Cuántas palabras comunes en español se leyeron: sirve para saber si la página está derecha."""
    return sum(1 for w in re.findall(r"[a-z]+", _sin_acentos(t).lower()) if w in _COMUNES)


def _leer_pagina(img, leer):
    """OCR de una página; si casi no sale español legible, prueba girada 90/270/180° (escaneos de lado o de cabeza)."""
    txt = leer(img)
    mejor, pm = txt, _puntaje(txt)
    if pm >= 3 or len(txt.strip()) < 5:  # bien leída, o página en blanco
        return txt
    for ang in (90, 270, 180):
        t = leer(img.rotate(ang, expand=True))
        p = _puntaje(t)
        if p > pm:
            mejor, pm = t, p
        if pm >= 3:
            break
    return mejor


def _recorte(img, y0, y1):
    w, h = img.size
    return img.crop((0, int(h * y0), w, int(h * y1)))


def _leer_inicio(img, leer):
    """OCR solo de la parte alta de la página. Si no sale español legible, prueba la página girada 90/270/180°
    (siempre sobre el recorte, que cuesta menos que la página entera).
    -> (texto, página ya derecha, lectura_confiable). Confiable=False si el recorte salió en blanco."""
    mejor = None
    for ang in (0, 90, 270, 180):
        im = img if ang == 0 else img.rotate(ang, expand=True)
        t = leer(_recorte(im, 0, FRAC_SUP))
        if ang == 0 and len(t.strip()) < 5:
            return t, img, False
        p = _puntaje(t)
        if mejor is None or p > mejor[0]:
            mejor = (p, t, im)
        if p >= 2:
            break
    return mejor[1], mejor[2], True


def _leer_pagina_rapido(img, leer):
    """-> (texto, ya_no_es_dictamen). El segundo valor es True cuando la página salió derecha y legible pero sin el
    título: no hace falta leer el resto de la página ni la página siguiente."""
    t, im, ok = _leer_inicio(img, leer)
    if not ok:
        return _leer_pagina(img, leer), False  # recorte en blanco (margen grande): lectura completa de siempre
    if titulo_dictamen(t)[0]:
        return t + "\n" + leer(_recorte(im, FRAC_SUP - SOLAPE, 1.0)), False  # OV y fecha: el resto de la página
    if _puntaje(t) >= 2:
        return t, True
    return leer(im), False  # lectura dudosa (logo, foto, mala calidad): página completa, ya enderezada


def texto_ocr_pdf(origen, max_pag=2, parar_si=None):
    """Texto leído por OCR de las primeras páginas. origen = ruta (str) o bytes del PDF.
    parar_si(texto) -> True corta en cuanto lo leído hasta ahora ya basta (ahorra tiempo).
    Devuelve '' si no hay motor de OCR."""
    mo = _motor()
    if not mo:
        return ""
    import fitz
    d = fitz.open(origen) if isinstance(origen, str) else fitz.open(stream=origen, filetype="pdf")
    partes = []
    with d:
        for i in range(min(max_pag, len(d))):
            if RAPIDO:
                txt, no_dictamen = _leer_pagina_rapido(_imagen(d[i]), mo[1])
                partes.append(txt)
                if no_dictamen:
                    break
            else:
                partes.append(_leer_pagina(_imagen(d[i]), mo[1]))
            if parar_si and parar_si("\n".join(partes)):
                break
    return "\n".join(partes)


def hay_texto_legible(texto):
    """True si el OCR leyó algo que parece texto (y no solo ruido o una hoja en blanco)."""
    return len(re.findall(r"[A-Za-z]{2,}", texto or "")) >= 5


# ---------------- Coincidencia aproximada ----------------
_A_LETRAS = str.maketrans("0158", "olsb")  # confusiones típicas del OCR dentro de palabras


def _plano(t):
    return re.sub(r"[^a-z0-9]+", " ", _sin_acentos(t).lower()).strip()


def _palabras(t):
    return _plano(t).translate(_A_LETRAS).split()


def _buscar(frase, pal):
    """Mejor coincidencia aproximada de `frase` dentro de la lista de palabras -> (parecido 0-1, ini, fin).
    Prueba ventanas de n-1, n y n+1 palabras para tolerar palabras partidas o pegadas."""
    n, L = len(frase.split()), len(frase)
    mejor = (0.0, 0, 0)
    for k in {max(n - 1, 1), n, n + 1}:
        for i in range(len(pal) - k + 1):
            cand = " ".join(pal[i:i + k])
            if abs(len(cand) - L) > 0.3 * L:
                continue
            sm = difflib.SequenceMatcher(None, frase, cand)
            if sm.real_quick_ratio() <= mejor[0] or sm.quick_ratio() <= mejor[0]:
                continue
            r = sm.ratio()
            if r > mejor[0]:
                mejor = (r, i, i + k)
    return mejor


def titulo_dictamen(texto):
    """-> (es_dictamen, por_vivienda), tolerante a errores de OCR.
    Mismas reglas que el texto normal: el título 'Dictamen de Obra Terminada' va al inicio (o, si el PDF lo trae
    en otro orden, en cualquier parte junto con 'Fecha dictamen'). No cuentan las facturas (CFDI), las carátulas,
    ni el estado de cuenta que solo trae el concepto 'Dictamen de Obra Terminada (DOT)'."""
    pal = _palabras(texto)
    if len(pal) < 3:
        return False, False
    if any(_buscar(f, pal)[0] >= 0.85 for f in ("folio fiscal", "sello digital", "tipo de comprobante")):
        return False, False  # es un CFDI
    inicio = pal[:150]  # ~ primeros 800 caracteres
    r, _, fin = _buscar("dictamen de obra terminada", inicio)
    if r < UMBRAL:
        r, _, fin = _buscar("dictamen de obra terminada", pal)
        if r < UMBRAL or _buscar("fecha dictamen", pal)[0] < UMBRAL:
            return False, False
    if _buscar("caratula de reporte de obra", inicio)[0] >= 0.85:
        return False, False
    viv = _buscar("vivienda", pal[fin:fin + 4])[0] >= UMBRAL  # 'POR VIVIENDA' justo después del título
    if not viv and _buscar("estado de cuenta", pal)[0] >= 0.85:
        return False, False  # 'Dictamen de Obra Terminada' a secas dentro de un estado de cuenta
    return True, viv


# ---------------- Números y fechas leídos por OCR ----------------
_RE_NUM_RUIDO = re.compile(r"(?<![A-Za-z0-9])[0-9OoIl|SB]{11}(?![A-Za-z0-9])")
_TR_DIG = str.maketrans("OoIl|SB", "0011158")


def corrige_digitos(texto):
    """Arregla números de 11 cifras (la OV) con letras que el OCR confundió: 5029O667OO1 -> 50290667001."""
    def fix(m):
        s = m.group(0)
        return s.translate(_TR_DIG) if sum(c.isdigit() for c in s) >= 6 else s
    return _RE_NUM_RUIDO.sub(fix, texto)


def dictamen_completo(texto):
    """True si lo leído ya trae el título del dictamen y un número de OV: no hace falta leer más páginas."""
    return titulo_dictamen(texto)[0] and re.search(r"(?<!\d)\d{10,12}(?!\d)", corrige_digitos(texto)) is not None


_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]
_TR_ANIO = str.maketrans("olis", "0115")


def _mes_aprox(tok):
    """Número de mes (1-12) de una palabra leída por OCR, o 0 si no es un mes o la lectura es ambigua
    (p. ej. 'juiio' puede ser junio o julio: se descarta para no acomodar mal el archivo)."""
    tok = tok.translate(_A_LETRAS)
    if len(tok) < 4:
        return 0
    pts = sorted(((difflib.SequenceMatcher(None, tok, m).ratio(), i + 1) for i, m in enumerate(_MESES)), reverse=True)
    (r1, m1), (r2, _) = pts[0], pts[1]
    return m1 if r1 >= 0.75 and r1 - r2 >= 0.05 else 0


def _anio(tok):
    t = tok.translate(_TR_ANIO)
    return int(t) if re.fullmatch(r"20\d\d", t) else 0


def _fecha_en(toks):
    """Primer 'mes + año' (08 DE ABRIL DEL 2026) en una lista de palabras -> (mes, año) o (0, 0)."""
    for k, tk in enumerate(toks):
        mes = _mes_aprox(tk)
        if mes:
            a = next((x for x in map(_anio, toks[k + 1:k + 4]) if x), 0)
            if a:
                return mes, a
    return 0, 0


def fecha_dictamen(texto):
    """-> (mes, año, motivo) de la 'FECHA DICTAMEN' leída por OCR (tolera letras mal leídas)."""
    toks = _plano(texto).split()
    pal = [t.translate(_A_LETRAS) for t in toks]
    r, _, fin = _buscar("fecha dictamen", pal)
    if r >= UMBRAL:
        mes, a = _fecha_en(toks[fin:fin + 14])
        if mes:
            return mes, a, ""
    # etiqueta ilegible o separada de su valor: solo si en todo el texto hay UNA sola fecha
    fechas = {_fecha_en(toks[k:k + 5]) for k in range(len(toks))} - {(0, 0)}
    if len(fechas) == 1:
        mes, a = next(iter(fechas))
        return mes, a, ""
    return 0, 0, "No se encontró la 'Fecha dictamen' en el texto leído por OCR (DD DE MES DEL AAAA)"
