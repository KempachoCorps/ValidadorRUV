import hashlib, importlib.util, io, os, re, shutil, unicodedata, zipfile
import xml.etree.ElementTree as ET
import pandas as pd
import streamlit as st

import lector_pdf  # lectura de PDF en paralelo y con caché en disco (ver lector_pdf.py)
import ocr_pdf  # OCR para PDF escaneados (ver ocr_pdf.py)

_MALOS = re.compile(r'[<>:"|?*]')


def _norm(r):
    return "/".join(p.strip() for p in str(r or "").replace("\\", "/").split("/") if p.strip())


def _subcarpetas(root):
    out = []
    for dp, dn, _ in os.walk(root):
        dn.sort()
        out += [os.path.relpath(os.path.join(dp, d), root).replace("\\", "/") for d in dn]
    return sorted(out)


def _arbol(nombre, rutas):
    t = {}
    for r in rutas:
        n = t
        for s in r.split("/"):
            n = n.setdefault(s, {})
    lineas = [f"📁 {nombre}"]

    def rec(n, pref):
        ks = sorted(n, key=str.lower)
        for i, k in enumerate(ks):
            ult = i == len(ks) - 1
            lineas.append(f"{pref}{'└── ' if ult else '├── '}📁 {k}")
            rec(n[k], pref + ("    " if ult else "│   "))

    rec(t, "")
    return "\n".join(lineas)


def _carpeta_hom(elegir_carpeta, clave):
    """Selector de carpeta compartido entre Homologación y Llenado (session_state['carpeta_hom'])."""
    k = f"in_{clave}"
    pend = st.session_state.pop("_ruta_pend", None)
    if pend:
        st.session_state[k] = pend
    if k not in st.session_state:  # al entrar al módulo, toma la carpeta ya elegida
        st.session_state[k] = st.session_state.get("carpeta_hom", "")

    def _examinar():
        e = elegir_carpeta(st.session_state.get("carpeta_hom", ""))
        if e:
            st.session_state[k] = os.path.normpath(e)  # se escribe en el cuadro de texto mismo

    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    v = c1.text_input("Carpeta a homologar", key=k, placeholder=r"C:\Users\...\CORTE")
    c2.button("📂 Examinar", on_click=_examinar, use_container_width=True, key=f"ex_{clave}")
    v = v.strip().strip('"')
    st.session_state["carpeta_hom"] = os.path.normpath(v) if v else ""
    root = st.session_state["carpeta_hom"]
    if not root or not os.path.isdir(root):
        st.info("Selecciona una carpeta válida (con Examinar o pegando la ruta).")
        return None
    return root


def _mover_archivo(src, dst):
    """Mueve src a dst SIN dejar copias. En el mismo disco solo renombra (os.rename). Si el archivo
    está abierto en otro programa (p. ej. Acrobat) falla con un mensaje claro y no toca nada.
    Solo si el destino está en otro disco copia y borra, y si no puede borrar el original deshace la copia."""
    try:
        os.rename(src, dst)
        return
    except PermissionError:
        raise PermissionError("el archivo está abierto en otro programa; ciérralo e intenta de nuevo")
    except (FileExistsError, FileNotFoundError):
        raise
    except OSError:
        pass  # otro disco: se copia y se borra el original
    shutil.copy2(src, dst)
    try:
        os.remove(src)
    except OSError:
        os.remove(dst)  # no dejar la copia si no se pudo quitar el original
        raise PermissionError("no se pudo quitar el original (¿abierto en otro programa?); no se movió")


# ---------------- Homologación ----------------
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre"]
SUBS = ["VO - FACTURA", "DOT"]
ESTRUCTURA_OV = [r for m in MESES for r in [m] + [f"{m}/{x}" for x in SUBS]]

# Acepta: "123", "OV 123", "OV123", "123-texto extra", "OV 123 texto extra"
_RE_OV = re.compile(r"(?:OV\s*)?(\d+)(?:[\s\-_.].*)?", re.I | re.S)


def _zip_estructura(root):
    """ZIP solo con las carpetas (sin archivos) de la estructura actual."""
    base = os.path.basename(root) or "estructura"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(base + "/", "")
        for dp, dn, _ in os.walk(root):
            for d in dn:
                z.writestr(f"{base}/{os.path.relpath(os.path.join(dp, d), root).replace(os.sep, '/')}/", "")
    return buf.getvalue()


def modulo_homologacion(elegir_carpeta):
    root = _carpeta_hom(elegir_carpeta, "hom")
    if not root:
        return
    filas = []
    for h in sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))):
        m = _RE_OV.fullmatch(h)
        if not m:
            filas.append((h, h, "Omitida (no es un número de OV)"))
            continue
        nuevo = f"OV {m.group(1)}"
        if re.fullmatch(r"OV\s*\d+", h, re.I):
            filas.append((h, h, "Ya tiene OV: solo se completa la estructura"))
        else:
            filas.append((h, nuevo, "Se renombra y se crea la estructura"))
    validas = [f for f in filas if not f[2].startswith("Omitida")]
    if not validas:
        st.warning("No encontré carpetas con número de OV dentro de la carpeta seleccionada.")
        return
    izq, der = st.columns([3, 2])
    with izq:
        st.markdown(f"**{len(validas)} carpeta(s) de OV** detectadas")
        st.dataframe(pd.DataFrame(filas, columns=["Carpeta actual", "Nueva carpeta", "Acción"]),
                     hide_index=True, use_container_width=True)
    with der:
        st.markdown("**Estructura de cada OV**")
        n0 = validas[0][1]
        st.code(_arbol(n0, ESTRUCTURA_OV), language=None)
    if st.button(f"Homologar {len(validas)} carpeta(s)", type="primary"):
        log = []
        for act, nue, acc in validas:
            src, dst = os.path.join(root, act), os.path.join(root, nue)
            try:
                if act != nue:
                    if os.path.exists(dst):
                        log.append({"Carpeta": act, "Resultado": f"❌ ya existe '{nue}'"})
                        continue
                    os.rename(src, dst)
                for r in ESTRUCTURA_OV:
                    os.makedirs(os.path.join(dst, r), exist_ok=True)
                log.append({"Carpeta": act, "Resultado": f"✅ {nue}"})
            except Exception as e:
                log.append({"Carpeta": act, "Resultado": f"❌ {type(e).__name__}: {e}"})
        st.session_state["hom_log"] = log
        st.rerun()
    if st.session_state.get("hom_log"):
        log = st.session_state["hom_log"]
        ok = sum(1 for x in log if x["Resultado"].startswith("✅"))
        st.success(f"Listo: {ok} de {len(log)} carpeta(s) homologadas.")
        with st.expander("Detalle"):
            st.dataframe(pd.DataFrame(log), hide_index=True, use_container_width=True)
    st.download_button("⬇️ Descargar estructura (ZIP, solo carpetas)", _zip_estructura(root),
                       f"{os.path.basename(root) or 'estructura'}.zip", mime="application/zip")


# ---------------- Llenado: manual (por título) ----------------
def _titulo(nombre):
    t = unicodedata.normalize("NFKD", os.path.splitext(nombre)[0]).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\s*(\(\d+\)|-?\s*copia(\s*\(\d+\))?|-?\s*copy)\s*$", "", t)
    t = re.sub(r"[\d_\-.\s]+", " ", t).strip()  # sin números (OV, fechas, consecutivos)
    return t or "(sin título)"


def _llenado_manual(root):
    destinos = _subcarpetas(root)
    if not destinos:
        st.warning("La carpeta no tiene subcarpetas. Crea la estructura primero en **Homologación**.")
        return
    solo_raiz = st.checkbox("Solo PDF sueltos en la raíz (no tocar los que ya están en subcarpetas)")
    if solo_raiz:
        pdfs = [os.path.join(root, f) for f in os.listdir(root) if f.lower().endswith(".pdf")]
    else:
        pdfs = [os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs if f.lower().endswith(".pdf")]
    grupos = {}
    for p in sorted(pdfs):
        grupos.setdefault(_titulo(os.path.basename(p)), []).append(p)
    if not grupos:
        st.info("No se encontraron PDF.")
        return
    reglas = st.session_state.setdefault("reglas", {})
    df = pd.DataFrame([{"Título": t, "Archivos": len(ps), "Ejemplo": os.path.basename(ps[0]),
                        "Carpeta destino": reglas.get(t, "") if reglas.get(t, "") in destinos else ""}
                       for t, ps in sorted(grupos.items(), key=lambda x: -len(x[1]))])
    st.markdown(f"**{len(pdfs)} PDF** agrupados en **{len(grupos)} títulos**. Asigna la carpeta destino de cada título:")
    ed = st.data_editor(
        df, hide_index=True, use_container_width=True, disabled=["Título", "Archivos", "Ejemplo"],
        column_config={"Carpeta destino": st.column_config.SelectboxColumn(options=[""] + destinos)},
        key=f"llen_tabla_{root}_{solo_raiz}")
    for t, d in zip(ed["Título"], ed["Carpeta destino"]):
        reglas[t] = d or ""
    sim = st.checkbox("Solo simular (no mueve nada)", value=True)
    if st.button("Acomodar archivos", type="primary"):
        log = []
        for t, ps in grupos.items():
            d = reglas.get(t)
            if not d:
                continue
            for p in ps:
                carpeta = os.path.join(root, d)
                if os.path.normcase(os.path.dirname(p)) == os.path.normcase(carpeta):
                    continue
                dst, k = os.path.join(carpeta, os.path.basename(p)), 1
                b, ext = os.path.splitext(dst)
                while os.path.exists(dst):
                    dst, k = f"{b} ({k}){ext}", k + 1
                try:
                    if not sim:
                        _mover_archivo(p, dst)
                    est = "✅"
                except Exception as e:
                    est = f"❌ {type(e).__name__}: {e}"
                log.append({"Estado": est, "Archivo": os.path.relpath(p, root), "Destino": os.path.relpath(dst, root)})
        sin = sum(len(ps) for t, ps in grupos.items() if not reglas.get(t))
        (st.info if sim else st.success)(
            f"{'Simulación: se moverían' if sim else 'Movidos'} {len(log)} archivo(s). Sin carpeta asignada: {sin}.")
        if log:
            st.dataframe(pd.DataFrame(log), hide_index=True, use_container_width=True)


# ---------------- Llenado: facturas y estados de cuenta (automático) ----------------
_RE_NUM_OV = re.compile(r"(?<!\d)\d{10,12}(?!\d)")                          # 50290667001
_RE_MES_4 = re.compile(r"(?<![\d/])(0[1-9]|1[0-2])/(20\d{2})(?![\d/])")      # 04/2026
_RE_MES_2 = re.compile(r"(?<![\d/])(0[1-9]|1[0-2])/(2\d)(?![\d/])")          # 04/26


def _carpetas_ov(root):
    """{numero: nombre_de_carpeta} de las carpetas de OV que están directamente en la raíz."""
    out = {}
    for d in sorted(os.listdir(root), key=lambda x: (not x.upper().startswith("OV"), x)):
        if os.path.isdir(os.path.join(root, d)):
            m = _RE_OV.fullmatch(d)
            if m:
                out.setdefault(m.group(1), d)
    return out


# Los tres lectores viven en lector_pdf.py: caché en memoria y en disco (sobrevive a reiniciar la app y a mover
# el archivo de carpeta). `mtime` se conserva solo por compatibilidad con las llamadas existentes.
def _texto_pdf(ruta, mtime=None):
    """Texto interno con pypdf (primeras 30 páginas)."""
    return lector_pdf.texto("pypdf", ruta)


def _texto_fitz(ruta, mtime=None):
    """Segunda lectura con PyMuPDF (texto interno + texto ordenado por renglón). '' si no está instalado."""
    return lector_pdf.texto("fitz", ruta)


def _texto_ocr(ruta, mtime=None):
    """OCR de un PDF escaneado (2 primeras páginas; se detiene al hallar título de dictamen + OV)."""
    return lector_pdf.texto("ocr", ruta)


def _es_escaneado(*textos):
    """True si el PDF no trae texto útil (todo es imagen): lo poco que haya son sellos, folios o ruido."""
    return len(re.findall(r"[A-Za-z]", " ".join(textos))) < 40


def _es_factura(texto):
    """'Factura' como título (al inicio del texto) o, si el PDF lo trae en otro orden,
    la palabra en cualquier parte junto con datos típicos de un CFDI."""
    if re.search(r"\bfactura\b", texto[:400], re.I):
        return True
    return bool(re.search(r"\bfactura\b", texto, re.I)
                and re.search(r"tipo de comprobante|folio fiscal", texto, re.I))


SUB_ESTADO = SUBS[0]  # subcarpeta del mes para los estados de cuenta: VO - FACTURA
SUB_CARATULA = SUBS[0]  # las carátulas van en OV/Mes/VO - FACTURA
SUB_DICTAMEN = SUBS[1]  # los Dictámenes de Obra Terminada por Vivienda van en OV/Mes/DOT

_MES_ABREV = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
              "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}
_F = r"(\d{1,2})\s*(?:[-/.]|de)\s*([A-Za-zÁÉÍÓÚáéíóúñÑ]{3,10}|\d{1,2})\s*(?:[-/.]|de)\s*(\d{4}|\d{2})"
_RE_PERIODO = re.compile(rf"periodo\s*:?\s*{_F}\s*(?:al|a|hasta)\s*{_F}", re.I)   # PERIODO: 11-ABR-2026 AL 10-MAY-2026
_RE_RANGO = re.compile(rf"{_F}\s+(?:al|hasta)\s+{_F}", re.I)                      # 11/04/2026 AL 10/05/2026


def _num_mes(tok):
    if tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 12 else 0
    t = unicodedata.normalize("NFKD", tok).encode("ascii", "ignore").decode().lower()[:3]
    return _MES_ABREV.get(t, 0)


def _mes_periodo(texto):
    """-> (mes, año, motivo) del PRIMER mes del periodo 'DD-MMM-AAAA AL DD-MMM-AAAA' (o formatos similares)."""
    m = _RE_PERIODO.search(texto) or _RE_RANGO.search(texto)
    if not m:
        return 0, 0, "No se encontró el periodo (DD-MMM-AAAA AL DD-MMM-AAAA)"
    mes = _num_mes(m.group(2))
    if not mes:
        return 0, 0, f"Mes no reconocido en el periodo: {m.group(2)}"
    a = int(m.group(3))
    return mes, (a + 2000 if a < 100 else a), ""


def _es_estado_cuenta(texto, solo_titulo=False):
    if re.search(r"estado\s+de\s+cuenta", texto[:400], re.I):
        return True
    return (not solo_titulo) and bool(re.search(r"estado\s+de\s+cuenta", texto, re.I)
                                      and (_RE_PERIODO.search(texto) or _RE_RANGO.search(texto)))


def _sin_acentos(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


_RE_CARATULA = re.compile(r"caratula\s+de\s+reporte\s+de\s+obra")
_RE_ORDEN_VERIF = re.compile(r"orden\s+de\s+verif\w*\.?\s*:?\s*(\d{10,12})")
# "01 AL 15 DE ABRIL DEL 2026"  o  "26 DE MARZO AL 10 DE ABRIL DEL 2026"
# Los espacios son opcionales (\s*) porque el PDF a veces los pega: "16 AL 31DE JULIO DEL 2026"
_RE_PER_REPORTE = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*de\s*([a-z]{3,10}))?\s*al\s*(\d{1,2})\s*de\s*([a-z]{3,10})\s*(?:del?\s*)?(\d{4})")


# "Dictamen de Obra Terminada" (con o sin "por Vivienda"); no cuenta el concepto "Dictamen de Obra Terminada (DOT)" del estado de cuenta
_RE_DICTAMEN = re.compile(r"dictamen\s+de\s+obra\s+terminada(?!\s*\(\s*dot)")
_RE_DICT_VIV = re.compile(r"dictamen\s+de\s+obra\s+terminada\s+por\s+vivienda")
_RE_ETIQ_FECHA_DICT = re.compile(r"fecha\s+(?:del?\s+)?dictamen")
_RE_FECHA_DICT = re.compile(r"(?<!\d)(\d{1,2})\s*de\s*([a-z]{3,10})\s*(?:del?\s*)?(\d{4})")
_RE_FECHA_DICT_NUM = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2}|[a-z]{3,10})\s*[-/.]\s*(\d{4}|\d{2})(?!\d)")


def _es_dictamen(texto):
    """Título 'Dictamen de Obra Terminada' (con o sin 'por Vivienda') al inicio o, si el PDF lo trae en otro
    orden, en cualquier parte junto con 'Fecha dictamen'. No lo son: las facturas (CFDI) que mencionen el
    concepto, ni el estado de cuenta, que solo trae el concepto 'Dictamen de Obra Terminada (DOT)'."""
    t = _sin_acentos(texto).lower()
    if re.search(r"folio\s+fiscal|tipo\s+de\s+comprobante|sello\s+digital|\buuid\b", t):
        return False  # es un CFDI (factura)
    if not _RE_DICTAMEN.search(t):
        return False
    if not _RE_DICT_VIV.search(t) and re.search(r"estado\s+de\s+cuenta", t):
        return False  # "Dictamen de Obra Terminada" a secas dentro de un estado de cuenta
    if _RE_DICTAMEN.search(t[:800]):
        return True
    return bool(_RE_ETIQ_FECHA_DICT.search(t))


def _mes_dictamen(texto, ocr=False):
    """-> (mes, año, motivo) de la 'FECHA DICTAMEN' (08 DE ABRIL DEL 2026). Solo se usa esa fecha.
    ocr=True: el texto viene de OCR (puede traer letras mal leídas) y se usa la lectura tolerante."""
    if ocr:
        mes, anio, motivo = ocr_pdf.fecha_dictamen(texto)
        return mes, anio, motivo
    t = _sin_acentos(texto).lower()

    def _ym(mes_tok, anio_tok):
        mes = _num_mes(mes_tok)
        if not mes:
            return 0, 0, f"Mes no reconocido en la fecha del dictamen: {mes_tok}"
        a = int(anio_tok)
        return mes, (a + 2000 if a < 100 else a), ""

    # 1) justo después de la etiqueta "FECHA DICTAMEN"
    for lab in _RE_ETIQ_FECHA_DICT.finditer(t):
        ventana = t[lab.end():lab.end() + 120]
        m = _RE_FECHA_DICT.search(ventana)
        if m:
            return _ym(m.group(2), m.group(3))
        m = _RE_FECHA_DICT_NUM.search(ventana)
        if m:
            return _ym(m.group(2), m.group(3))
    # 2) etiqueta separada de su valor: solo si en todo el PDF hay UNA sola fecha "DD DE MES DEL AAAA"
    fechas = {m.groups() for m in _RE_FECHA_DICT.finditer(t)}
    if len(fechas) == 1:
        _, mes_tok, anio_tok = next(iter(fechas))
        return _ym(mes_tok, anio_tok)
    return 0, 0, "No se encontró la 'Fecha dictamen' (DD DE MES DEL AAAA)"


def _es_caratula(texto):
    """Título 'Carátula de reporte de obra' (al inicio) o, si el PDF lo trae en otro orden,
    el título en cualquier parte junto con 'Periodo de reporte'."""
    t = _sin_acentos(texto).lower()
    if _RE_CARATULA.search(t[:800]):
        return True
    return bool(_RE_CARATULA.search(t) and re.search(r"periodo\s+de\s+reporte", t))


def _mes_caratula(texto):
    """-> (mes, año, motivo) del 'PERIODO DE REPORTE' (01 AL 15 DE ABRIL DEL 2026).
    Regla de negocio: mes VENCIDO = el mes más tardío del periodo, aunque las fechas vengan al revés
    (26 DE MARZO AL 10 DE ABRIL -> Abril; 10 DE ABRIL AL 26 DE MARZO -> Abril).
    Excepción: si el periodo cruza de diciembre a enero (26 DE DICIEMBRE AL 10 DE ENERO) se usa Diciembre.
    Solo se usa esa fecha: se ignoran Fecha de inicio programado, Term. prog., Inicio real, etc.
    (Los estados de cuenta usan otra regla: el mes de inicio; ver _mes_periodo.)"""
    t = _sin_acentos(texto).lower()

    def _de(m):
        tok_ini, tok_fin = m.group(2) or m.group(4), m.group(4)  # sin mes al inicio = mismo mes que el final
        m_ini, m_fin = _num_mes(tok_ini), _num_mes(tok_fin)
        if not m_ini or not m_fin:
            return 0, 0, f"Mes no reconocido en el periodo de reporte: {tok_ini if not m_ini else tok_fin}"
        a = int(m.group(5))
        if m_ini == 12 and m_fin == 1:  # 26 DE DICIEMBRE AL 10 DE ENERO DEL 2026: se guarda en Diciembre (del año anterior)
            return 12, a - 1, ""
        return max(m_ini, m_fin), a, ""  # al revés (10 DE ENERO AL 26 DE DICIEMBRE) también da Diciembre

    def _numerico(ventana):  # 26/03/2026 AL 10/04/2026 (en cualquier orden): toma el mes más tardío
        m = _RE_RANGO.search(ventana)
        if not m:
            return None
        fechas = []
        for tok_mes, tok_anio in ((m.group(2), m.group(3)), (m.group(5), m.group(6))):
            mes = _num_mes(tok_mes)
            if not mes:
                return 0, 0, f"Mes no reconocido en el periodo de reporte: {tok_mes}"
            a = int(tok_anio)
            fechas.append((a + 2000 if a < 100 else a, mes))
        (a1, m1), (a2, m2) = sorted(fechas)
        if m1 == 12 and m2 == 1 and a2 == a1 + 1:  # 26/12/2025 AL 10/01/2026: se guarda en Diciembre
            return m1, a1, ""
        return m2, a2, ""

    # 1) justo después de la etiqueta "PERIODO DE REPORTE"
    for lab in re.finditer(r"periodo\s+de\s+reporte", t):
        ventana = t[lab.end():lab.end() + 200]
        m = _RE_PER_REPORTE.search(ventana)
        if m:
            return _de(m)
        r = _numerico(ventana)  # formato numérico, solo dentro de la ventana de la etiqueta
        if r:
            return r
    # 2) si el PDF separó la etiqueta de su valor: solo el formato "DD AL DD DE MES DEL AAAA",
    #    que no se confunde con las otras fechas de la carátula (10/11/2025, etc.)
    m = _RE_PER_REPORTE.search(t)
    if m:
        return _de(m)
    return 0, 0, "No se encontró el 'Periodo de reporte' (DD AL DD DE MES DEL AAAA)"


def _datos_pdf(texto, ovs, ocr=False):
    """-> (tipo, estado, carpeta_ov, mes, anio, motivo).
    tipo: 'factura' | 'estado' | 'caratula' | 'dictamen' | ''.  estado: 'ok' | 'revisar' | 'ignorar'.
    ocr=True: el texto viene de OCR de un PDF escaneado. Ahí solo se reconoce el Dictamen de Obra Terminada
    (con o sin 'por Vivienda'), con una lectura tolerante a errores; lo demás queda 'por revisar'."""
    if ocr:
        texto = ocr_pdf.corrige_digitos(texto)
        if not ocr_pdf.titulo_dictamen(texto)[0]:
            return "", "revisar", "", "", "", "Escaneado: el OCR no lo reconoció como Dictamen de Obra Terminada"
        tipo = "dictamen"
    elif not texto.strip():
        return "", "revisar", "", "", "", "Sin texto (¿PDF escaneado?)"
    elif _es_caratula(texto):
        tipo = "caratula"
    elif _es_estado_cuenta(texto, solo_titulo=True):
        tipo = "estado"
    elif _es_dictamen(texto) and not _es_estado_cuenta(texto):
        tipo = "dictamen"
    elif _es_factura(texto):
        tipo = "factura"
    elif _es_estado_cuenta(texto):
        tipo = "estado"
    else:
        return "", "ignorar", "", "", "", "No es factura, estado de cuenta, carátula ni dictamen"
    cand = set(_RE_NUM_OV.findall(texto))
    if tipo in ("caratula", "dictamen"):
        etiq = _RE_ORDEN_VERIF.search(_sin_acentos(texto).lower())
        if etiq:
            cand = {etiq.group(1)}
    hay = sorted(n for n in cand if n in ovs)
    if len(hay) > 1:
        return tipo, "revisar", "", "", "", "Varias OV en el PDF: " + ", ".join(hay)
    if not hay:
        motivo = ("OV sin carpeta en la raíz: " + ", ".join(sorted(cand))) if cand else "No se encontró número de OV"
        return tipo, "revisar", "", "", "", motivo
    ov = ovs[hay[0]]
    if tipo == "dictamen":
        mes, anio, motivo = _mes_dictamen(texto, ocr)
        if not mes:
            return tipo, "revisar", ov, "", "", motivo
        return tipo, "ok", ov, MESES[mes - 1], anio, ""
    if tipo == "caratula":
        mes, anio, motivo = _mes_caratula(texto)
        if not mes:
            return tipo, "revisar", ov, "", "", motivo or "No se encontró el periodo de reporte"
        return tipo, "ok", ov, MESES[mes - 1], anio, ""
    if tipo == "estado":
        mes, anio, motivo = _mes_periodo(texto)
        if not mes:
            return tipo, "revisar", ov, "", "", motivo
        return tipo, "ok", ov, MESES[mes - 1], anio, ""
    meses = ({(int(m), int(a)) for m, a in _RE_MES_4.findall(texto)}
             | {(int(m), 2000 + int(a)) for m, a in _RE_MES_2.findall(texto)})
    if not meses:
        return tipo, "revisar", ov, "", "", "No se encontró el mes (MM/AAAA)"
    if len(meses) > 1:
        lista = ", ".join(f"{m:02d}/{a}" for m, a in sorted(meses, key=lambda x: (x[1], x[0])))
        return tipo, "revisar", ov, "", "", "Varios meses en el PDF: " + lista
    m, a = next(iter(meses))
    return tipo, "ok", ov, MESES[m - 1], a, ""


def _sub_en_lugar(p, root, nombres_ov_lc):
    """Si el archivo está EXACTAMENTE en  OV/Mes/VO - FACTURA/  u  OV/Mes/DOT/  devuelve esa subcarpeta;
    si no, None. (Sin distinguir mayúsculas.)"""
    rel = os.path.relpath(p, root).replace("\\", "/").split("/")
    if len(rel) != 4:
        return None
    ov, mes, sub, _ = rel
    if ov.lower() in nombres_ov_lc and mes.lower() in {x.lower() for x in MESES} and sub.lower() in {x.lower() for x in SUBS}:
        return sub
    return None


def _archivos_a_revisar(root, ovs, incluir_ov, ext, omitir_en_lugar=False):
    """Archivos con la extensión dada. Con omitir_en_lugar=True se saltan los que ya están en
    OV/Mes/VO - FACTURA o OV/Mes/DOT: así no se vuelven a abrir ni a leer."""
    nombres_ov = set(ovs.values())
    nombres_lc = {v.lower() for v in nombres_ov}
    out = []
    for dp, dn, fs in os.walk(root):
        if not incluir_ov and os.path.normpath(dp) == os.path.normpath(root):
            dn[:] = [d for d in dn if d not in nombres_ov]  # no entrar a las carpetas OV
        dn.sort()
        for f in sorted(fs):
            if not f.lower().endswith(ext):
                continue
            p = os.path.join(dp, f)
            if omitir_en_lugar and _sub_en_lugar(p, root, nombres_lc):
                continue
            out.append(p)
    return out


def _contar_en_lugar(root, ovs):
    """(PDF, XML) que ya están en su carpeta OV/Mes/VO - FACTURA o OV/Mes/DOT (no se leen)."""
    nombres_lc = {v.lower() for v in ovs.values()}
    cuenta = []
    for ext in (".pdf", ".xml"):
        cuenta.append(sum(1 for p in _archivos_a_revisar(root, ovs, True, ext) if _sub_en_lugar(p, root, nombres_lc)))
    return tuple(cuenta)


def _facturas_en_lugar(root, ovs):
    """PDF ya acomodados en OV/Mes/VO - FACTURA. No se reclasifican; solo sirven para ligar XML sueltos
    por sus sellos (su texto se lee únicamente si hay XML por acomodar)."""
    nombres_lc = {v.lower() for v in ovs.values()}
    out = []
    for p in _archivos_a_revisar(root, ovs, True, ".pdf"):
        sub = _sub_en_lugar(p, root, nombres_lc)
        if sub and sub.lower() == SUBS[0].lower():
            rel = os.path.relpath(p, root).replace("\\", "/").split("/")
            out.append({"pdf": p, "tipo": "factura", "estado": "ok", "ov": rel[0], "mes": rel[1], "anio": "",
                        "motivo": "", "dst_dir": os.path.dirname(p)})
    return out


def _precargar_pdf(archivos, ovs, progreso=None):
    """Lee en paralelo (y deja en caché) todo lo que _planear_facturas va a pedir, en las mismas 3 etapas:
    1) pypdf a todos; 2) PyMuPDF a los que no se identificaron; 3) OCR a los escaneados.
    Después el recorrido normal de _planear_facturas solo encuentra texto ya leído."""
    def avisa(fase):
        return (lambda h, t: progreso(fase, h, t)) if progreso else None
    lector_pdf.precargar("pypdf", archivos, avisa("Leyendo PDF"))
    pend2, tipo1 = [], {}
    for p in archivos:
        try:
            tipo, est = _datos_pdf(_texto_pdf(p), ovs)[:2]
        except Exception:
            continue
        if est == "ignorar" or not tipo:
            pend2.append(p)
            tipo1[p] = tipo
    lector_pdf.precargar("fitz", pend2, avisa("Segunda lectura"))
    if not (pend2 and ocr_pdf.ocr_disponible()):
        return
    esc = []
    for p in pend2:
        try:
            t1, t2 = _texto_pdf(p), _texto_fitz(p)
            alt = _datos_pdf(t2, ovs) if t2 else None
            tipo = alt[0] if (alt and alt[1] != "ignorar") else tipo1[p]
            if not tipo and _es_escaneado(t1, t2):
                esc.append(p)
        except Exception:
            continue
    lector_pdf.precargar("ocr", esc, avisa("OCR de escaneados"))


def _planear_facturas(root, ovs, incluir_ov, omitir_en_lugar=False, progreso=None):
    plan = []
    archivos = _archivos_a_revisar(root, ovs, incluir_ov, ".pdf", omitir_en_lugar)
    _precargar_pdf(archivos, ovs, progreso)
    for p in archivos:
        ocr = False  # True si se identificó leyendo la imagen del PDF (escaneado)
        try:
            mt = os.path.getmtime(p)
            t1 = _texto_pdf(p, mt)
            fuente = t1
            tipo, est, ov, mes, anio, motivo = _datos_pdf(t1, ovs)
            t2 = ""
            if est == "ignorar" or not tipo:  # segunda oportunidad con otro lector de PDF
                t2 = _texto_fitz(p, mt)
                alt = _datos_pdf(t2, ovs) if t2 else None
                if alt and alt[1] != "ignorar":
                    tipo, est, ov, mes, anio, motivo = alt
                    fuente = t2
            if not tipo and _es_escaneado(t1, t2) and ocr_pdf.ocr_disponible():
                # PDF escaneado (solo imagen): se lee con OCR para poder identificar el título del dictamen
                txt = _texto_ocr(p, mt)
                if not ocr_pdf.hay_texto_legible(txt):
                    est, motivo = "revisar", "Escaneado: el OCR no pudo leer texto (¿imagen borrosa, muy clara o en blanco?)"
                else:
                    r = _datos_pdf(txt, ovs, ocr=True)
                    if r[0]:
                        tipo, est, ov, mes, anio, motivo = r
                        fuente, ocr = txt, True
                    else:
                        est, motivo = "revisar", r[5]
            elif not tipo and _es_escaneado(t1, t2):
                motivo += " — instala un motor de OCR para leerlo (ver ocr_pdf.py)"
        except Exception as e:
            tipo, est, ov, mes, anio, motivo = "", "revisar", "", "", "", f"No se pudo leer ({type(e).__name__})"
            fuente = ""
        variante = ""
        if tipo == "dictamen":  # con o sin 'por Vivienda' (los dos van a la carpeta DOT)
            viv = ocr_pdf.titulo_dictamen(fuente)[1] if ocr else bool(_RE_DICT_VIV.search(_sin_acentos(fuente).lower()))
            variante = "por Vivienda" if viv else ""
        sub = {"factura": SUBS[0], "estado": SUB_ESTADO, "caratula": SUB_CARATULA,
               "dictamen": SUB_DICTAMEN}.get(tipo, "")
        dst_dir = os.path.join(*[q for q in (root, ov, mes, sub) if q]) if est == "ok" else ""
        plan.append({"pdf": p, "tipo": tipo, "estado": est, "ov": ov, "mes": mes, "anio": anio,
                     "motivo": motivo, "dst_dir": dst_dir, "ocr": ocr, "variante": variante})
    return plan


# ---------------- Llenado: XML (match por SelloCFD y SelloSAT) ----------------
@st.cache_data(show_spinner=False)
def _datos_xml(ruta, mtime):
    """UUID, SelloCFD y SelloSAT de un CFDI (acepta TFD 1.0 'selloCFD' y 1.1 'SelloCFD')."""
    raiz = ET.parse(ruta).getroot()
    sello_cfd = raiz.get("Sello") or raiz.get("sello") or ""
    sello_sat = uuid = ""
    for el in raiz.iter():
        if isinstance(el.tag, str) and el.tag.split("}")[-1] == "TimbreFiscalDigital":
            a = {k.lower(): v for k, v in el.attrib.items()}
            uuid, sello_sat = a.get("uuid", ""), a.get("sellosat", "")
            sello_cfd = a.get("sellocfd") or sello_cfd
            break
    return {"uuid": uuid, "sello_cfd": sello_cfd, "sello_sat": sello_sat}


def _sello_en_texto(sello, texto_norm, n=20, minimo=0.6):
    """True si el sello (base64) aparece en el texto del PDF sin espacios ni saltos de línea.
    Se compara por piezas de n caracteres para tolerar cortes de línea que el PDF mete en medio."""
    s = re.sub(r"\s+", "", sello or "")
    if not s:
        return False
    piezas = [s[i:i + n] for i in range(0, max(len(s) - n + 1, 1), n)]
    fallos_max = len(piezas) * (1 - minimo)  # fallos tolerados; al pasarse ya no puede haber coincidencia
    fallos = 0
    for p in piezas:
        if p not in texto_norm:
            fallos += 1
            if fallos > fallos_max + 1e-9:
                return False
    return True


def _planear_xml(xmls, plan_pdf):
    cand = []
    for x in plan_pdf:
        if x["tipo"] == "factura":
            try:
                cand.append((x, re.sub(r"\s+", "", _texto_pdf(x["pdf"], os.path.getmtime(x["pdf"])))))
            except Exception:
                pass
    out = []
    for xp in xmls:
        it = {"xml": xp, "estado": "revisar", "ov": "", "mes": "", "dst_dir": "", "pdf_ref": "", "motivo": ""}
        out.append(it)
        try:
            d = _datos_xml(xp, os.path.getmtime(xp))
        except Exception as e:
            it["motivo"] = f"XML ilegible ({type(e).__name__})"
            continue
        if not d["sello_cfd"] or not d["sello_sat"]:
            it["motivo"] = "Sin SelloCFD/SelloSAT (¿no es un CFDI timbrado?)"
            continue
        coinc = [x for x, n in cand if _sello_en_texto(d["sello_cfd"], n) and _sello_en_texto(d["sello_sat"], n)]
        if not coinc:
            it["motivo"] = "Ningún PDF coincide con su SelloCFD y SelloSAT"
            continue
        ok = [x for x in coinc if x["estado"] == "ok"]
        dsts = {x["dst_dir"] for x in ok}
        if len(dsts) > 1:
            it["motivo"] = "Coincide con varios PDF que van a carpetas distintas"
        elif not ok:
            it["motivo"] = "Su factura PDF no se pudo clasificar: " + coinc[0]["motivo"]
        else:
            x = ok[0]
            it.update(estado="ok", ov=x["ov"], mes=x["mes"], dst_dir=x["dst_dir"], pdf_ref=x["pdf"])
    return out


def _mover_facturas(root, items, sim):
    log = []
    for x in items:
        src = x.get("pdf") or x["xml"]
        dst, k = os.path.join(x["dst_dir"], os.path.basename(src)), 1
        b, ext = os.path.splitext(dst)
        while os.path.exists(dst):
            dst, k = f"{b} ({k}){ext}", k + 1
        try:
            if not sim:
                os.makedirs(x["dst_dir"], exist_ok=True)
                _mover_archivo(src, dst)
            est = "✅"
        except Exception as e:
            est = f"❌ {type(e).__name__}: {e}"
        log.append({"Estado": est, "Archivo": os.path.relpath(src, root), "Destino": os.path.relpath(dst, root)})
    return log


def _firma_carpeta(root, ovs):
    """Huella de los PDF/XML de la raíz (ruta, tamaño, fecha) y de las carpetas OV: si no cambia, el plan sigue valiendo."""
    h = hashlib.md5(repr(sorted(ovs.items())).encode("utf-8", "replace"))
    for dp, dn, fs in os.walk(root):
        dn.sort()
        for f in sorted(fs):
            if f.lower().endswith((".pdf", ".xml")):
                p = os.path.join(dp, f)
                try:
                    st_ = os.stat(p)
                    h.update(f"{os.path.relpath(p, root)}|{st_.st_size}|{st_.st_mtime_ns}\n".encode("utf-8", "replace"))
                except OSError:
                    pass
    return h.hexdigest()


def _llenado_facturas(root):
    if importlib.util.find_spec("pypdf") is None:
        st.error("Falta instalar la librería para leer PDF: `pip install pypdf`")
        return
    ovs = _carpetas_ov(root)
    if not ovs:
        st.warning("No hay carpetas de OV en la raíz. Crea la estructura primero en **Homologación**.")
        return
    if ocr_pdf.ocr_disponible():
        st.caption(f"🔎 OCR activo ({ocr_pdf.descripcion_motor()}): los PDF escaneados se leen por imagen para identificar el "
                   "Dictamen de Obra Terminada (con o sin 'por Vivienda'). Es más lento que un PDF con texto.")
    else:
        st.caption("⚠️ OCR no instalado: los PDF escaneados (solo imagen) quedarán 'por revisar'. Para leerlos instala "
                   "`pip install rapidocr-onnxruntime` (o Tesseract con español + `pip install pytesseract`) y reinicia la app.")

    r = st.session_state.get("fac_log")
    if r and r["root"] == root:
        ok = sum(1 for x in r["log"] if x["Estado"] == "✅")
        (st.info if r["sim"] else st.success)(
            f"{'Simulación: se moverían' if r['sim'] else 'Movidos'} {ok} de {len(r['log'])} archivo(s).")
        with st.expander("Detalle del último proceso"):
            st.dataframe(pd.DataFrame(r["log"]), hide_index=True, use_container_width=True)

    incluir = st.checkbox("Incluir también los archivos que ya están dentro de carpetas OV", value=True, key="fac_incluir",
                          help="Activado: revisa TODA la carpeta raíz, sin importar en qué ruta estén los archivos; los que ya están en su lugar solo se cuentan. Desactívalo para revisar únicamente lo que está fuera de las carpetas OV.")
    revisar_todo = st.checkbox("Volver a revisar también los archivos que ya están en su carpeta (más lento)", value=False,
                               key="fac_revisar_todo",
                               help="Desactivado (más rápido): los PDF y XML que ya están en OV/Mes/VO - FACTURA u OV/Mes/DOT no se "
                                    "vuelven a leer ni a validar. Actívalo para comprobar que también esos estén en la OV y el mes correctos.")
    omitir = not revisar_todo
    def _calcular():
        barra = st.progress(0.0, text="Leyendo PDF y XML...")

        def _prog(fase, hechos, total):
            barra.progress(min(hechos / max(total, 1), 1.0), text=f"{fase}: {hechos} de {total}")
        xmls = _archivos_a_revisar(root, ovs, incluir, ".xml", omitir)
        plan = _planear_facturas(root, ovs, incluir, omitir, _prog)
        plan_todos = _planear_facturas(root, ovs, True, omitir, _prog) if (xmls and not incluir) else plan
        if xmls and omitir:  # las facturas ya acomodadas solo se leen si hay XML sueltos que ligar
            ya = _facturas_en_lugar(root, ovs)
            lector_pdf.precargar("pypdf", [x["pdf"] for x in ya], lambda h, t: _prog("Leyendo facturas ya acomodadas", h, t))
            plan_todos = plan_todos + ya
        plan_xml = _planear_xml(xmls, plan_todos)
        n_lugar = _contar_en_lugar(root, ovs) if omitir else (0, 0)
        barra.empty()
        return plan, plan_xml, n_lugar

    # El plan se conserva mientras no cambie nada en la carpeta: marcar casillas, abrir el diagnóstico o cambiar
    # de pestaña ya no vuelve a recorrer ni a clasificar todos los archivos.
    firma = (root, incluir, omitir, _firma_carpeta(root, ovs))
    memo = st.session_state.get("_memo_llenado")
    if not memo or memo[0] != firma:
        memo = (firma, _calcular())
        st.session_state["_memo_llenado"] = memo
    plan, plan_xml, (n_pdf_lugar, n_xml_lugar) = memo[1]
    if n_pdf_lugar or n_xml_lugar:
        st.caption(f"⚡ {n_pdf_lugar} PDF y {n_xml_lugar} XML ya están en su carpeta (OV/Mes/VO - FACTURA o DOT) y no se volvieron a leer.")
    if not plan and not plan_xml:
        if n_pdf_lugar or n_xml_lugar:
            st.success("Todo lo encontrado ya está acomodado en su carpeta; no hay nada por mover.")
        else:
            st.info("No se encontraron PDF ni XML" + ("." if incluir else " fuera de las carpetas OV. Activa la casilla de arriba para revisar también el interior de las carpetas OV."))
        return

    por_mover, xml_mover = [], []
    if plan:
        etiqueta = {"factura": "Factura", "estado": "Estado de cuenta", "caratula": "Carátula de reporte de obra",
                    "dictamen": "Dictamen de Obra Terminada", "": ""}

        def _tipo(x):  # 'Dictamen de Obra Terminada' o 'Dictamen de Obra Terminada por Vivienda'
            return etiqueta[x["tipo"]] + (" " + x["variante"] if x.get("variante") else "")
        listas = [x for x in plan if x["estado"] == "ok"]
        revisar = [x for x in plan if x["estado"] == "revisar"]
        ignor = [x for x in plan if x["estado"] == "ignorar"]
        por_mover = [x for x in listas if os.path.normcase(os.path.dirname(x["pdf"])) != os.path.normcase(x["dst_dir"])]
        nf = sum(1 for x in listas if x["tipo"] == "factura")
        nc = sum(1 for x in listas if x["tipo"] == "caratula")
        nd = sum(1 for x in listas if x["tipo"] == "dictamen")
        no = sum(1 for x in listas if x.get("ocr"))
        st.markdown(f"**{len(plan)} PDF** revisados: **{len(listas)}** identificados ({nf} facturas, "
                    f"{len(listas) - nf - nc - nd} estados de cuenta, {nc} carátulas, {nd} dictámenes; "
                    f"{len(listas) - len(por_mover)} ya en su lugar{f'; {no} leídos por OCR' if no else ''}), "
                    f"**{len(revisar)}** por revisar, {len(ignor)} no son factura, estado de cuenta, carátula ni dictamen.")
        if por_mover:
            st.dataframe(pd.DataFrame([{"PDF": os.path.relpath(x["pdf"], root), "Tipo": _tipo(x),
                                        "Lectura": "OCR" if x.get("ocr") else "Texto",
                                        "OV": x["ov"], "Mes": x["mes"], "Año": x["anio"],
                                        "Destino": os.path.relpath(x["dst_dir"], root)}
                                       for x in por_mover]), hide_index=True, use_container_width=True)
            if any(x.get("ocr") for x in por_mover):
                st.caption("Los PDF con Lectura = OCR se identificaron leyendo la imagen; revisa su OV y mes en la tabla "
                           "antes de mover (la simulación está activada por defecto).")
        if revisar:
            st.warning("Estos PDF no se moverán hasta resolver el motivo:")
            st.dataframe(pd.DataFrame([{"PDF": os.path.relpath(x["pdf"], root), "Tipo": _tipo(x),
                                        "Motivo": x["motivo"]} for x in revisar]),
                         hide_index=True, use_container_width=True)
        if ignor:
            with st.expander(f"{len(ignor)} PDF que no son factura, estado de cuenta, carátula ni dictamen (se dejan donde están)"):
                st.dataframe(pd.DataFrame({"PDF": [os.path.relpath(x["pdf"], root) for x in ignor]}),
                             hide_index=True, use_container_width=True)

    if plan_xml:
        xok = [x for x in plan_xml if x["estado"] == "ok"]
        xrev = [x for x in plan_xml if x["estado"] == "revisar"]
        xml_mover = [x for x in xok if os.path.normcase(os.path.dirname(x["xml"])) != os.path.normcase(x["dst_dir"])]
        st.markdown(f"**{len(plan_xml)} XML** revisados: **{len(xok)}** con su factura "
                    f"({len(xok) - len(xml_mover)} ya en su lugar), **{len(xrev)}** por revisar.")
        if xml_mover:
            st.dataframe(pd.DataFrame([{"XML": os.path.relpath(x["xml"], root),
                                        "Factura (PDF)": os.path.basename(x["pdf_ref"]), "OV": x["ov"], "Mes": x["mes"],
                                        "Destino": os.path.relpath(x["dst_dir"], root)} for x in xml_mover]),
                         hide_index=True, use_container_width=True)
        if xrev:
            st.warning("Estos XML no se moverán hasta resolver el motivo:")
            st.dataframe(pd.DataFrame([{"XML": os.path.relpath(x["xml"], root), "Motivo": x["motivo"]} for x in xrev]),
                         hide_index=True, use_container_width=True)

    if plan:
        with st.expander("🔍 Ver el texto que se leyó de un PDF (diagnóstico)"):
            sel = st.selectbox("PDF", [os.path.relpath(x["pdf"], root) for x in plan], key="fac_dbg")
            pdbg = os.path.join(root, sel)
            txt = _texto_pdf(pdbg, os.path.getmtime(pdbg))
            if _es_escaneado(txt, _texto_fitz(pdbg, os.path.getmtime(pdbg))) and ocr_pdf.ocr_disponible():
                st.caption("PDF escaneado: este es el texto leído por OCR.")
                txt = _texto_ocr(pdbg, os.path.getmtime(pdbg))
            st.text_area("Inicio (primeros 1500 caracteres)", txt[:1500], height=200)
            st.text_area("Final (donde suelen ir los sellos)", txt[-1500:], height=200)

    mover = por_mover + xml_mover
    if not mover:
        return
    sim = st.checkbox("Solo simular (no mueve nada)", value=True, key="fac_sim")
    partes = ([f"{len(por_mover)} factura(s)"] if por_mover else []) + ([f"{len(xml_mover)} XML"] if xml_mover else [])
    if st.button("Mover " + " y ".join(partes), type="primary", key="fac_btn"):
        st.session_state["fac_log"] = {"root": root, "sim": sim, "log": _mover_facturas(root, mover, sim)}
        st.rerun()


def modulo_llenado(elegir_carpeta):
    root = _carpeta_hom(elegir_carpeta, "llen")
    if not root:
        return
    modo = st.radio("Modo", ["🧾 Facturas y estados de cuenta", "📄 Otros PDF (manual)"], horizontal=True,
                    label_visibility="collapsed", key="llen_modo")
    if modo.startswith("🧾"):
        _llenado_facturas(root)
    else:
        _llenado_manual(root)
