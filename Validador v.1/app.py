import base64, datetime, functools, hashlib, io, json, os, re, sqlite3, subprocess, sys, threading, unicodedata
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote
from collections import Counter
import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from modulos import modulo_homologacion, modulo_llenado
import ocr_pdf  # OCR para PDF escaneados (ver ocr_pdf.py)

DB = "index.db"

MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


# ---------- Utilidades de texto ----------
def sin_acentos(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()


def nums(s):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.\d+|\d+", s or "")]


def montos(s):
    """Solo importes con dos decimales (64,959.26 / 0.00); ignora enteros sueltos como 7 o 560."""
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.\d{2}(?!\d)", s or "")]


def parse_periodo(txt):
    """Devuelve ((d,m,a),(d,m,a)) o None. Tolera '01-AGO-2026 AL 31-AGO-2026',
    '11 Junio a 10 Julio 2026', '31 DE JUNIO AL 30 DE JULIO 2026', etc.
    Usa tuplas (no datetime) para no fallar con fechas inexistentes (ej. 31 de junio)."""
    t = sin_acentos(txt).lower()
    f = []
    for d, m, y in re.findall(
        r"(\d{1,2})[\s\-/.]*(?:de\s+)?([a-z]{3,10})\.?(?:[\s\-/.]*(?:de\s+)?(\d{4}))?", t
    ):
        if m[:3] in MESES:
            f.append([int(d), MESES[m[:3]], int(y) if y else None])
    if len(f) < 2:
        return None
    a, b = f[:2]
    if a[2] is None and b[2]:
        a[2] = b[2] - 1 if a[1] > b[1] else b[2]
    if b[2] is None and a[2]:
        b[2] = a[2] + 1 if b[1] < a[1] else a[2]
    return tuple(a), tuple(b)


def _f(d):
    return f"{d[0]:02d}/{d[1]:02d}/{d[2] or '?'}"


def mostrar(ok, valor_pdf):
    if ok:
        return "✅ OK"
    return f"❌ No coincide (PDF: {valor_pdf if valor_pdf is not None else 'no se encontró'})"


_FECHA = r"\d{1,2}[\s\-/.]*(?:de\s+)?[a-z]{3,10}\.?(?:[\s\-/.]*(?:de\s+)?\d{4})?"


def buscar_periodo(texto):
    """Busca 'fecha AL fecha' en cualquier parte del texto; si no, prueba junto a 'PERIODO'."""
    t = sin_acentos(texto).lower()
    for m in re.finditer(rf"({_FECHA})\s+(?:al|a)\s+({_FECHA})", t):
        p = parse_periodo(m.group(0))
        if p:
            return p
    m = re.search(r"periodo\s*:?(.{0,120})", t, re.S)
    return parse_periodo(m.group(1)) if m else None


# ---------- Extracción del PDF ----------
def _lineas(page, tol=6):
    """Texto ordenado por posición en la página (renglón por renglón, izquierda a derecha)."""
    w = sorted(page.get_text("words"), key=lambda t: ((t[1] + t[3]) / 2, t[0]))
    filas = []
    for t in w:
        yc = (t[1] + t[3]) / 2
        if filas and abs(yc - filas[-1][0]) <= tol:
            filas[-1][1].append(t)
        else:
            filas.append([yc, [t]])
    return "\n".join(" ".join(x[4] for x in sorted(f, key=lambda t: t[0])) for _, f in filas)


_OV = r"(?<!\d)\d{11}(?!\d)"  # 11 dígitos, aunque vengan pegados a letras (OV50297954001)
_DOT = r"dictamen\s+de\s+obra\s+terminada"


def _huella_txt(t):
    """Misma huella = mismo contenido (ignora mayúsculas, acentos y espacios)."""
    n = re.sub(r"\s+", " ", sin_acentos(t)).lower().strip()
    return "t:" + hashlib.sha256(n.encode()).hexdigest()


def _huella_nombre(ctx):
    """Para PDF sin texto: nombre sin extensión ni sufijos '(1)', 'copia', 'copy'."""
    nombre = re.sub(r"\.pdf$", "", ctx.rsplit("/", 1)[-1], flags=re.I)
    nombre = re.sub(r"\s*(\(\d+\)|-?\s*copia(\s*\(\d+\))?|-?\s*copy)\s*$", "", nombre, flags=re.I)
    return "n:" + sin_acentos(nombre).lower().strip()


def _ovs(conjunto):
    c = sorted(set(conjunto))
    return f" {' '.join(c)} " if c else None


def _extraer_escaneado(pdf_bytes, ctx, ctx_n, ov_ctx):
    """PDF sin texto interno (escaneado). Si hay motor de OCR se lee la imagen y se busca el título
    'Dictamen de Obra Terminada' (con o sin 'por Vivienda'), tolerando errores de lectura.
    Sin OCR (o si no logra leer nada) se conserva el criterio anterior: solo por el nombre del archivo."""
    por_nombre = bool(re.search(_DOT, ctx_n))
    huella = _huella_nombre(ctx)  # un escaneado no tiene texto confiable: los duplicados se detectan por nombre
    if not ocr_pdf.ocr_disponible():
        if por_nombre:
            return {"motivo": "dictamen de obra terminada (por nombre, sin texto)", "tipo": "dot",
                    "ov": _ovs(ov_ctx), "huella": huella}
        return {"motivo": "sin texto (¿escaneado?)", "tipo": "otro", "ov": _ovs(ov_ctx), "huella": huella}
    try:
        txt = ocr_pdf.texto_ocr_pdf(pdf_bytes, max_pag=2, parar_si=ocr_pdf.dictamen_completo)
    except Exception:
        txt = ""
    if not ocr_pdf.hay_texto_legible(txt):  # el OCR no sacó nada útil
        if por_nombre:
            return {"motivo": "dictamen de obra terminada (por nombre; OCR sin texto legible)", "tipo": "dot",
                    "ov": _ovs(ov_ctx), "huella": huella}
        return {"motivo": "sin texto (OCR no legible)", "tipo": "otro", "ov": _ovs(ov_ctx), "huella": huella}
    ovs = set(re.findall(_OV, ocr_pdf.corrige_digitos(txt))) | set(ov_ctx)
    es_dot, viv = ocr_pdf.titulo_dictamen(txt)
    if es_dot or por_nombre:
        return {"motivo": "dictamen de obra terminada" + (" por vivienda" if viv else "") + " (OCR)", "tipo": "dot",
                "ov": _ovs(ovs), "huella": huella}
    return {"motivo": "escaneado (OCR): no es un Dictamen de Obra Terminada", "tipo": "otro", "ov": _ovs(ovs),
            "huella": huella}


def extraer(pdf_bytes, ctx="", max_pag=15):
    """Clasifica el PDF: 'estado' (Estado de cuenta), 'dot' (Dictamen de Obra Terminada) u 'otro'.
    ctx = ruta relativa y nombre del archivo (sirve para ligar la OV y para PDF sin texto)."""
    ctx_n = sin_acentos(ctx.replace("_", " ")).lower()
    ov_ctx = re.findall(_OV, ctx)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        n = min(max_pag, len(d))
        textos = [d[i].get_text() for i in range(n)]
        if not "".join(textos).strip():  # escaneado: todo es imagen
            return _extraer_escaneado(pdf_bytes, ctx, ctx_n, ov_ctx)
        todos = set(re.findall(_OV, " ".join(textos)))
        idx = [i for i, t in enumerate(textos) if re.search(r"estado\s+de\s+cuenta", sin_acentos(t).lower())]
        if not idx:
            ini = " ".join(textos[:2])
            if re.search(_DOT, sin_acentos(ini).lower()) or re.search(_DOT, ctx_n):
                return {"motivo": "dictamen de obra terminada", "tipo": "dot",
                        "ov": _ovs(set(re.findall(_OV, ini)) | set(ov_ctx)), "huella": _huella_txt(ini)}
            return {"motivo": "sin título 'Estado de cuenta'", "tipo": "otro", "ov": _ovs(todos | set(ov_ctx)),
                    "huella": _huella_txt(ini)}
        # página del título y la siguiente (el estado puede continuar), máx. 3 páginas
        sel = sorted({j for i in idx[:2] for j in (i, i + 1) if j < n})[:3]
        texto = "\n".join(textos[j] for j in sel)
        texto2 = "\n".join(_lineas(d[j]) for j in sel)
    ovs = set(re.findall(_OV, texto + " " + texto2))
    if not ovs:
        return {"motivo": "estado de cuenta sin OV legible", "tipo": "otro", "ov": _ovs(todos), "huella": _huella_txt(texto)}
    return {"motivo": "ok", "tipo": "estado", "ov": _ovs(ovs), "texto": texto, "texto2": texto2}


def analizar(*textos):
    """Obtiene Periodo, D.O.T y Total. textos = (orden interno del PDF, orden por posición)."""
    res = {"periodo": None, "dot": None, "total": None}
    for t in textos:
        if not t:
            continue
        if res["periodo"] is None:
            res["periodo"] = buscar_periodo(t)
        if res["dot"] is None:
            m = re.search(r"\(DOT\)(.*?)(?:\(DSC\)|Servicios\s+Conectados)", t, re.S)
            n = montos(m.group(1)) if m else []
            if len(n) >= 2:
                res["dot"] = n[1]  # costo, CANTIDAD, total
    for t in reversed(textos):  # el Total se lee primero del texto por posición
        if res["total"] is not None or not t:
            continue
        m = re.search(r"TOTAL\s+A\s+PAGAR(.{0,120})", t, re.I | re.S)
        n = montos(m.group(1))[:3] if m else []
        if n:
            res["total"] = max(n)
    return res


def a_num(x):
    if isinstance(x, (int, float)):
        return float(x)
    n = nums(str(x))
    return n[0] if n else None


# ---------- Índice local (SQLite) ----------
def db():
    con = sqlite3.connect(DB)
    con.execute("""create table if not exists docs(
        id text primary key, name text, path text, modified text,
        ov text, texto text, texto2 text, motivo text, tipo text, huella text)""")
    con.execute("create table if not exists meta(k text primary key, v text)")
    cols = [c[1] for c in con.execute("pragma table_info(docs)")]
    for extra in ("motivo", "tipo", "huella"):
        if extra not in cols:
            con.execute(f"alter table docs add column {extra} text")
    return con


def get_meta(k):
    r = db().execute("select v from meta where k=?", (k,)).fetchone()
    return r[0] if r else None


def indexar(root, bar):
    """Lee todos los PDF de la carpeta (y subcarpetas). Solo reprocesa los nuevos o modificados."""
    con = db()
    pdfs = [os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs if f.lower().endswith(".pdf")]
    actuales = set(pdfs)
    sobrantes = [(i,) for (i,) in con.execute("select id from docs") if i not in actuales]
    con.executemany("delete from docs where id=?", sobrantes)  # otras carpetas o archivos ya borrados
    for k, p in enumerate(pdfs):
        if k % 5 == 0 or k == len(pdfs) - 1:
            bar.progress((k + 1) / len(pdfs), text=f"{k + 1}/{len(pdfs)}  {os.path.basename(p)}")
        try:
            stt = os.stat(p)
            mod = f"{stt.st_mtime_ns}:{stt.st_size}"
            ya = con.execute("select modified, motivo, tipo, huella from docs where id=?", (p,)).fetchone()
            # los escaneados que se indexaron sin OCR se vuelven a leer una vez cuando ya hay un motor instalado
            sin_ocr = bool(ya and ya[1] and ya[1].startswith(("sin texto (¿escaneado?)", "dictamen de obra terminada (por nombre, sin texto)"))
                           and ocr_pdf.ocr_disponible())
            if ya and ya[0] == mod and ya[1] and ya[2] and not (ya[2] in ("dot", "otro") and not ya[3]) \
                    and not ya[1].startswith("error") and not sin_ocr:
                continue  # sin cambios
            with open(p, "rb") as f:
                d = extraer(f.read(), os.path.relpath(p, root).replace("\\", "/"))
        except Exception as e:
            mod = ""
            d = {"motivo": f"error al leer ({type(e).__name__})"}
        con.execute("insert or replace into docs values(?,?,?,?,?,?,?,?,?,?)", (
            p, os.path.basename(p), p, mod, d.get("ov"), d.get("texto"), d.get("texto2"),
            d.get("motivo"), d.get("tipo"), d.get("huella")))
    con.execute("insert or replace into meta values('root', ?)", (root,))
    con.commit()
    resumen = con.execute("select motivo, count(*) from docs group by motivo order by 2 desc").fetchall()
    return len(pdfs), resumen


# ---------- Carpeta local: selector y servidor para abrir los archivos ----------
def elegir_carpeta(inicial=""):
    """Abre el cuadro de Windows para elegir una carpeta (la app corre en tu propia PC)."""
    codigo = (
        "import tkinter as tk\nfrom tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        f"print(filedialog.askdirectory(initialdir={inicial!r}, title='Selecciona la carpeta de los archivos') or '')"
    )
    out = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    return out.stdout.strip()


class _Silencioso(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@st.cache_resource
def servidor(root):
    """Servidor local de solo lectura (127.0.0.1) para abrir los PDF y las carpetas desde la tabla."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Silencioso, directory=root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


# ---------- Validación ----------
def _existe(d):
    try:
        datetime.date(d[2] or 2000, d[1], d[0])
        return True
    except Exception:
        return False


def _rel(p, root):
    return os.path.relpath(p, root).replace("\\", "/")


def enlace_archivo(p, root, base):
    """Enlace (servidor local) para abrir el PDF en una pestaña del navegador."""
    if not p:
        return ""
    rel = _rel(p, root)
    return "" if rel.startswith("..") else f"{base}/{quote(rel)}"


_CARPETA_DOT = re.compile(r"^(d\.?\s*o\.?\s*t\.?|dictamenes? de obra terminada)(?![a-z0-9])")


def _en_carpeta_dot(p, root, ov):
    """True si el PDF está dentro de una carpeta llamada DOT (no cuenta VO - FACTURA ni otras)
    y pertenece a la OV: si la ruta nombra alguna OV, debe ser esta; si no, se usa la OV del contenido."""
    rel = _rel(p, root)
    if rel.startswith(".."):
        return False
    segs = [os.path.basename(root)] + rel.split("/")
    if not any(_CARPETA_DOT.match(sin_acentos(x).lower().strip()) for x in segs[:-1]):
        return False
    en_ruta = set(re.findall(_OV, "/".join(segs)))
    return ov in en_ruta if en_ruta else True


def _link_pdf(ruta, root, base):
    """Línea en Markdown: nombre del PDF (clic = abrirlo) y la subcarpeta donde está."""
    url = enlace_archivo(ruta, root, base)
    nombre = os.path.basename(ruta).replace("[", "(").replace("]", ")").replace("`", "'")
    sub = os.path.dirname(_rel(ruta, root)).replace("$", "\\$")
    txt = f"[`{nombre}`]({url})" if url else f"`{nombre}`"
    return txt + (f" — {sub}" if sub else "")


def carpeta_local(rutas, ov, root, base):
    """Enlace a la carpeta de la OV: el tramo de la ruta que contiene la OV (p. ej. 'OV 50290383001').
    Si ninguna carpeta la lleva en su nombre, usa la carpeta que contiene el primer PDF."""
    fallback = None
    for p in rutas:
        rel = _rel(p, root)
        if rel.startswith(".."):
            continue
        segs = rel.split("/")
        for k, seg in enumerate(segs[:-1]):  # el último es el archivo
            if ov in seg:
                return f"{base}/{quote('/'.join(segs[:k + 1]))}/"
        if fallback is None:
            fallback = segs[:-1]
    if fallback is None:
        return ""
    return f"{base}/{quote('/'.join(fallback))}/" if fallback else f"{base}/"


def validar(df, root, base):
    """Devuelve (tabla de resultados, lista de (OV, [motivos], [(título, [archivos])]))."""
    con = db()
    out, motivos = [], []
    for _, r in df.iterrows():
        ov = re.sub(r"\D", "", str(r["OV"]).split(".")[0])
        like = (f"% {ov} %",)
        p_xl = parse_periodo(r["Periodo"])
        x_d, x_t = a_num(r["D.O.T"]), a_num(r["Total"])
        filas = con.execute("select name,path,texto,texto2 from docs where ov like ? and motivo='ok'", like).fetchall()
        hits = [(n, u, analizar(t1, t2)) for n, u, t1, t2 in filas]
        if len(hits) > 1:  # misma OV en varios estados de cuenta: desempata por Periodo
            mismo = [x for x in hits if p_xl and x[2]["periodo"] == p_xl]
            if mismo:
                hits = mismo
        fila = {"OV": ov, "Carpeta": "", "Archivo": "", "Periodo": "", "D.O.T": "", "Carpeta D.O.T": "", "Total": ""}
        rutas = [x[0] for x in con.execute(
            "select path from docs where ov like ? and path is not null order by (tipo='estado') desc, (tipo='dot') desc", like)]
        fila["Carpeta"] = carpeta_local(rutas, ov, root, base)
        msgs, listas = [], []

        # --- Carpeta D.O.T: solo los PDF dentro de la carpeta DOT de la OV (VO - FACTURA no cuenta) ---
        cand = con.execute("select path, coalesce(huella, id), tipo, motivo from docs "
                           "where ov like ? and coalesce(tipo,'') != 'estado'", like).fetchall()
        en_dot = sorted((c for c in cand if _en_carpeta_dot(c[0], root, ov)),
                        key=lambda c: (len(os.path.basename(c[0])), c[0]))  # el original (nombre más corto) primero
        grupos = {}
        for p, h, t, mo in en_dot:
            grupos.setdefault(h, []).append((p, t, mo))
        n_dot = len(grupos)
        dup = len(en_dot) - n_dot  # duplicados omitidos
        esperado = round(x_d) if x_d is not None else None
        ok_c = esperado is not None and abs(x_d - esperado) < 1e-6 and n_dot == esperado
        fila["Carpeta D.O.T"] = (f"{'✅' if ok_c else '❌'} {n_dot}/{esperado if esperado is not None else '?'}"
                                 + (f" ({dup} duplicado{'s' if dup != 1 else ''} omitido{'s' if dup != 1 else ''})" if dup else ""))
        if not ok_c:
            if esperado is None or abs(x_d - esperado) > 1e-6:
                msgs.append(f"Carpeta D.O.T: el D.O.T del Excel no es un número entero, no se puede comparar (hay {n_dot} PDF en la carpeta DOT).")
            else:
                dif = esperado - n_dot
                det = f"faltan {dif}" if dif > 0 else f"sobran {-dif}"
                m = (f"Carpeta D.O.T: el Excel indica {esperado} y en la carpeta DOT de esta OV se encontraron {n_dot} PDF distintos ({det})"
                     f"{f', sin contar {dup} duplicado' + ('s' if dup != 1 else '') if dup else ''}.")
                if not en_dot:
                    m += " No se encontró ningún PDF dentro de una carpeta llamada DOT para esta OV."
                msgs.append(m)
            unicos = [g[0] for g in grupos.values()]  # (ruta, tipo, motivo)
            if esperado is not None and n_dot > esperado:
                patron = lambda p: re.sub(r"\d+", "#", sin_acentos(os.path.basename(p)).lower())
                cuenta = Counter(patron(u[0]) for u in unicos)
                comun = cuenta.most_common(1)[0][0]
                raro = lambda u: (len(cuenta) > 1 and patron(u[0]) != comun) or (u[1] == "otro" and "sin texto" not in (u[2] or ""))
                items = sorted(((raro(u), u[0]) for u in unicos), key=lambda x: (not x[0], x[1]))
                listas.append((f"PDF en la carpeta DOT de esta OV (esperados {esperado}); revisa los que no corresponden",
                               [("⚠️ " if r else "") + _link_pdf(p, root, base) for r, p in items]))
                if any(r for r, _ in items):
                    listas[-1][1].insert(0, "_⚠️ = nombre distinto al de la mayoría o contenido que no es un Dictamen de Obra Terminada; "
                                            "suelen ser los que sobran._")
            if dup:
                listas.append(("Duplicados omitidos (no se cuentan)",
                               [f"{_link_pdf(d[0], root, base)}  \n  igual a {_link_pdf(g[0][0], root, base)}"
                                for g in grupos.values() for d in g[1:]]))
            if esperado is not None and n_dot < esperado:
                dentro = {c[0] for c in en_dot}
                fuera = [c[0] for c in cand if c[2] == "dot" and c[0] not in dentro]
                if fuera:
                    listas.append(("Dictámenes con esta OV fuera de la carpeta DOT; revisa si deben estar ahí",
                                   [_link_pdf(p, root, base) for p in sorted(fuera)]))

        if not hits:
            fila["Estado"] = "PDF no encontrado"
            otros = con.execute("select motivo, count(*) from docs where ov like ? and motivo != 'ok' group by motivo", like).fetchall()
            if otros:
                det = "; ".join(f"{c} PDF: {m}" for m, c in otros)
                msgs.append(f"No hay un estado de cuenta legible con esta OV. Sí aparece en otros PDF ({det}).")
            else:
                msgs.append("Ningún PDF indexado contiene esta OV: no está en la carpeta seleccionada o el PDF es una imagen sin texto.")
        elif len(hits) > 1:
            fila["Estado"] = "Ambiguo (OV en varios PDF)"
            nombres = ", ".join(f"`{h[0]}`" for h in hits)
            msgs.append(f"La OV está en {len(hits)} estados de cuenta y no se pudo elegir uno por el Periodo: {nombres}")
        else:
            nombre, url, a = hits[0]
            per, dot, tot = a["periodo"], a["dot"], a["total"]
            ok_p = p_xl is not None and p_xl == per
            ok_d = dot is not None and x_d is not None and abs(x_d - dot) < 0.001
            ok_t = tot is not None and x_t is not None and abs(x_t - tot) < 0.01
            if not ok_p:
                if p_xl is None:
                    msgs.append(f"Periodo: no se pudo interpretar el valor del Excel ({r['Periodo']}).")
                elif per is None:
                    msgs.append("Periodo: no se pudo leer el periodo en el PDF.")
                else:
                    m = f"Periodo: el Excel dice {_f(p_xl[0])} al {_f(p_xl[1])} y el PDF dice {_f(per[0])} al {_f(per[1])}."
                    if not (_existe(p_xl[0]) and _existe(p_xl[1])):
                        m += " Ojo: el Excel trae una fecha que no existe en el calendario."
                    msgs.append(m)
            if not ok_d:
                msgs.append("D.O.T: el valor del Excel no es un número." if x_d is None else
                            "D.O.T: no se pudo leer en el PDF." if dot is None else
                            f"D.O.T: el Excel dice {x_d:.2f} y el PDF dice {dot:.2f}.")
            if not ok_t:
                msgs.append("Total: el valor del Excel no es un número." if x_t is None else
                            "Total: no se pudo leer en el PDF." if tot is None else
                            f"Total: el Excel dice {x_t:,.2f} y el PDF dice {tot:,.2f}.")
            fila.update({
                "Archivo": enlace_archivo(url, root, base),
                "Periodo": mostrar(ok_p, f"{_f(per[0])} al {_f(per[1])}" if per else None),
                "D.O.T": mostrar(ok_d, f"{dot:.2f}" if dot is not None else None),
                "Total": mostrar(ok_t, f"{tot:,.2f}" if tot is not None else None),
                "Estado": "OK" if ok_p and ok_d and ok_t and ok_c else "Con diferencias"})
        if msgs:
            motivos.append((ov, msgs, listas))
        fila.pop("Estado", None)  # la columna Estado se sustituyó por Carpeta
        out.append(fila)
    return pd.DataFrame(out), motivos


# ---------- UI ----------
st.set_page_config(page_title="Validación de OV", layout="wide")


def _logo(nombre):
    """Etiqueta <img> con el logo; si el archivo no está en la carpeta assets, no muestra nada (no falla)."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", nombre)
    try:
        with open(ruta, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError:
        return "<span style='width:64px'></span>"
    return f'<img src="data:image/png;base64,{b64}" style="height:64px;width:auto">'


def _limpia(r):
    r = (r or "").strip().strip('"')
    return os.path.normpath(r) if r else ""


def _examinar():
    elegida = elegir_carpeta(st.session_state.get("ruta", ""))
    if elegida:
        st.session_state["ruta"] = os.path.normpath(elegida)


# Barra lateral más compacta (sin scroll)
st.markdown("""<style>
[data-testid="stSidebarContent"] {position: relative;}
[data-testid="stSidebarHeader"] {position: absolute; top: 0.55rem; right: 0.6rem; width: auto; height: 2.2rem;
    padding: 0; margin: 0; z-index: 5; display: flex; align-items: center;}
[data-testid="stSidebarUserContent"] {padding-top: 0.55rem;}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {gap: 0.4rem;}
[data-testid="stSidebar"] [data-testid="stTextInput"] label {margin-bottom: 0;}
[data-testid="stSidebar"] div[data-baseweb="input"] input {padding-top: 0.3rem; padding-bottom: 0.3rem;}
</style>""", unsafe_allow_html=True)

# ---------- Menú horizontal de módulos ----------
st.markdown("""<style>
div[data-testid="stRadio"] > div[role="radiogroup"] {gap: 0.4rem; border-bottom: 2px solid rgba(128,128,128,.3); padding-bottom: .4rem;}
div[data-testid="stRadio"] label {padding: .35rem 1.1rem; border-radius: .5rem; cursor: pointer; font-weight: 600;}
div[data-testid="stRadio"] label:has(input:checked) {background: rgba(255,75,75,.15);}
div[data-testid="stRadio"] label > div:first-child {display: none;}
</style>""", unsafe_allow_html=True)
# conserva el valor de la ruta del Validador aunque se navegue a otro módulo
if "ruta" in st.session_state:
    st.session_state["ruta"] = st.session_state["ruta"]
modulo = st.radio("Módulo", ["Validador", "Homologación", "Llenado"], horizontal=True,
                  key="modulo", label_visibility="collapsed")

st.markdown(
    f"""<div style="display:flex;align-items:center;justify-content:space-between;gap:24px;margin:0 0 12px 0">
        {_logo('infonavit.png')}
        <h1 style="margin:0;padding:0;flex:1;text-align:center;font-size:2rem">{'Validación de OV' if modulo == 'Validador' else modulo}</h1>
        {_logo('ruv.png')}
    </div>""",
    unsafe_allow_html=True,
)

if "ruta" not in st.session_state:
    st.session_state["ruta"] = get_meta("root") or ""

if modulo == "Homologación":
    modulo_homologacion(elegir_carpeta)
    st.stop()
if modulo == "Llenado":
    modulo_llenado(elegir_carpeta)
    st.stop()

with st.sidebar:
    st.markdown(
        """<div style="display:flex;align-items:center;gap:10px;height:2.2rem;margin:0 0 0.9rem 0">
            <span style="font-size:1.6rem;line-height:1">📁</span>
            <span style="font-size:1.5rem;font-weight:600">Carpeta de archivos</span></div>""",
        unsafe_allow_html=True,
    )
    st.text_input("Ruta de la carpeta", key="ruta", placeholder=r"C:\Users\...\CORTE 04092026")
    c1, c2 = st.columns(2)
    c1.button("📂 Examinar", on_click=_examinar, use_container_width=True)
    if c2.button("Indexar PDFs", use_container_width=True):
        ruta_i = _limpia(st.session_state["ruta"])
        if not ruta_i or not os.path.isdir(ruta_i):
            st.error("La carpeta no existe. Revisa la ruta o usa Examinar.")
        else:
            try:
                n, _ = indexar(ruta_i, st.progress(0))
                st.success(f"{n} PDF en la carpeta")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
    if ocr_pdf.ocr_disponible():
        st.caption(f"🔎 OCR activo ({ocr_pdf.descripcion_motor()}): los PDF escaneados se leen por imagen.")
    else:
        st.caption("⚠️ OCR no instalado: los PDF escaneados solo se reconocen por su nombre. "
                   "Instala `pip install rapidocr-onnxruntime` (o Tesseract + `pip install pytesseract`) y reinicia la app.")
    _idx = get_meta("root")
    if _idx and _limpia(_idx) != _limpia(st.session_state["ruta"]):
        st.caption("⚠️ Esta carpeta aún no está indexada. Pulsa **Indexar PDFs**.")

archivo = st.file_uploader("Cargar archivo", type=["xlsx", "xls"])
if archivo and st.button("Validar", type="primary"):
    ruta = _limpia(st.session_state["ruta"])
    if not ruta or not os.path.isdir(ruta):
        st.error("Selecciona primero la carpeta de los archivos (barra izquierda).")
        st.stop()
    if _limpia(get_meta("root")) != ruta:
        st.error("Esta carpeta aún no está indexada. Pulsa **Indexar PDFs** en la barra izquierda.")
        st.stop()
    base = servidor(ruta)
    df = pd.read_excel(archivo, dtype={"OV": str})
    df.columns = [c.strip() for c in df.columns]
    res, motivos = validar(df, ruta, base)
    res = res.fillna("").astype(str)
    st.dataframe(
        res, use_container_width=True, hide_index=True,
        column_config={
            "Carpeta": st.column_config.LinkColumn("Carpeta", display_text="📁"),
            "Archivo": st.column_config.LinkColumn("Archivo", display_text="📄"),
        },
    )
    with st.container(border=True):
        st.markdown("**Motivos de diferencia**")
        if not motivos:
            st.success("Todas las filas coinciden con su estado de cuenta.")
        for ov, msgs, listas in motivos:
            st.markdown(f"**OV {ov}**")
            st.markdown("\n".join(f"- {m.replace('$', chr(92) + '$')}" for m in msgs))
            for titulo, items in listas:
                with st.expander(f"{titulo} ({sum(1 for i in items if not i.startswith('_'))})"):
                    st.markdown("\n".join(i if i.startswith("_") else f"- {i}" for i in items))

    # Excel descargable: los enlaces apuntan a las rutas reales de tu equipo
    def _a_local(u):
        return os.path.normpath(os.path.join(ruta, unquote(u[len(base) + 1:].rstrip("/"))))

    exp = res.copy()
    for col, icono in (("Carpeta", "📁"), ("Archivo", "📄")):
        exp[col] = exp[col].map(lambda u, i=icono: f'=HYPERLINK("{_a_local(u)}","{i}")' if u else "")
    buf = io.BytesIO()
    exp.to_excel(buf, index=False)
    st.download_button("Descargar resultado", buf.getvalue(), "validacion.xlsx")
