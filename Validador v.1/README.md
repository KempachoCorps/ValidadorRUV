# Validador v.1

Aplicación local en Streamlit para validar y homologar documentos PDF.

## Ejecución

Desde este directorio, crea y activa un entorno virtual, instala las dependencias y ejecuta la aplicación:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

La aplicación crea `index.db` para su índice local y `lector_pdf.py` puede crear `.cache_lectura_pdf.sqlite3`; ambos archivos son datos locales y están excluidos de Git. El OCR de PDF escaneados es opcional y requiere instalar RapidOCR o Tesseract por separado.
