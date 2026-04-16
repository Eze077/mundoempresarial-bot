# Manual de Estilo y SEO - MundoEmpresarial.ar

## Identidad editorial

**MundoEmpresarial.ar** es un medio digital de noticias de negocios orientado a pymes, monotributistas, profesionales independientes y empresarios argentinos. El tono es **informativo, directo y accesible**: explicamos la coyuntura en lenguaje simple para quien toma decisiones de negocio.

---

## 1. Estructura de la nota

Toda nota publicada debe respetar la siguiente estructura:

### 1.1 Bajada / Lead (primer parrafo)

- Va en **negrita**.
- Resume en 1-2 oraciones el hecho central y por que importa al lector pyme.
- Maximo 280 caracteres.
- Debe contener el **keyword de enfoque**.

### 1.2 Subtitulos H2

- El **primer H2** se coloca inmediatamente despues del lead.
- Debe incluir el **keyword de enfoque** (requerimiento de Rank Math).
- Formato recomendado: `"{Keyword}: lo que necesitas saber"` o una frase que resuma la seccion.
- Los **H2 siguientes** deben ser descriptivos del contenido de la seccion, no genericos.
- **Frecuencia:** un H2 cada 2-4 parrafos (300-500 palabras aprox.), nunca mas de 5 parrafos seguidos sin subtitulo.
- **Prohibido:** H2 genericos vacios como "Mas detalles", "En profundidad" o "Contexto" sin relacion con el contenido.

### 1.3 Parrafos

- Maximo **3-4 oraciones** por parrafo.
- Si un parrafo supera las 80 palabras, dividirlo.
- Usar **oraciones cortas** (25 palabras maximo ideal).
- Un parrafo = una idea. No mezclar temas.

### 1.4 Citas y declaraciones

- Las declaraciones textuales van en parrafo propio.
- Formato: comillas tipograficas + atribucion.
- Ejemplo: *"Milei ya cumplio. Entro en desgaste", dijo una fuente presente en el evento.*
- Si hay multiples fuentes, separarlas en parrafos distintos.

### 1.5 Datos y cifras

- Los numeros y datos duros van en **negrita** para facilitar el escaneo.
- Ejemplo: *La inflacion de marzo fue del **3,4%**, la mas alta en 6 meses.*
- Si hay 3+ datos, usar **lista con vinetas** en vez de parrafo corrido.

### 1.6 Recuadro "Resumen para Pymes"

- Se inserta antes del cierre de la nota.
- Maximo **240 caracteres**.
- Lenguaje ultra simple: que paso y como impacta a una pyme.
- Estilo visual: fondo celeste (#eaf4fb), borde izquierdo azul (#1a6fa8).

### 1.7 Fuente

- Siempre al final de la nota.
- Formato: *Fuente: [Ver nota original](URL)* con `rel="noopener noreferrer"`.

---

## 2. Titulares

| Regla | Valor |
|-------|-------|
| Largo maximo | **60 caracteres** (Rank Math / Google SERP) |
| Corte | En limite de palabra, nunca a mitad de palabra |
| Puntos suspensivos | **No usar**. Si no entra, reformular |
| Formato | Oracion (solo primera letra mayuscula, salvo nombres propios) |
| Keyword | Debe aparecer en las primeras 5 palabras |
| Numeros | Usar digitos, no letras ("5 claves", no "cinco claves") |

**Buenos ejemplos:**
- *AFIP extiende el plazo para monotributistas*
- *Dolar hoy: el blue cerro a $1.450 y el cepo sigue*
- *Vaca Muerta bate record: 5 claves del boom energetico*

**Malos ejemplos:**
- ~~Patricia, la favorita entre los empresarios de Amcham: "Milei ya cumplio"~~ (77 caracteres, excede el limite)
- ~~IMPORTANTE: Nueva normativa que cambia todo para las pymes~~ (clickbait)

---

## 3. SEO on-page

### 3.1 Keyword de enfoque (Focus Keyword)

- **Una sola palabra** o frase corta.
- Se extrae del titulo: la primera palabra significativa (no stop-word, >3 caracteres).
- Debe aparecer en:
  - Titulo (primeras 5 palabras)
  - Primer H2
  - Meta description
  - Primer parrafo (lead)
  - Slug URL
  - Alt text de la imagen

### 3.2 Meta description

| Regla | Valor |
|-------|-------|
| Largo | **120-155 caracteres** |
| Keyword | Debe estar presente. Si no esta en el excerpt, se antepone: `"Keyword: descripcion..."` |
| Corte | En limite de palabra + "..." |
| Contenido | Resumir el hecho + incluir un gancho para el click |

### 3.3 Slug (URL)

| Regla | Valor |
|-------|-------|
| Largo maximo | **50 caracteres** |
| Formato | Minusculas, sin tildes, separado por guiones |
| Corte | En ultimo guion antes del limite |
| Contenido | Solo palabras significativas del titulo |

Ejemplo: `patricia-bullrich-amcham-milei-cumplio`

### 3.4 Rank Math - Campos meta

```
rank_math_title:           {Titulo SEO, 60 chars max}
rank_math_description:     {Meta description, 120-155 chars}
rank_math_focus_keyword:   {Keyword unico}
rank_math_robots:          ["index", "follow"]
rank_math_og_content_image: {URL de imagen destacada}
```

---

## 4. Categorias

Las notas se clasifican automaticamente en hasta **3 categorias** por relevancia. El titulo tiene **triple peso** en la deteccion.

| ID | Categoria | Keywords principales |
|----|-----------|---------------------|
| 95 | AFIP | afip, arca, impuesto, monotributo, iva, ganancias |
| 88 | Agro | agro, campo, soja, trigo, cosecha, siembra |
| 1048 | Coberturas | seguro, aseguradora, poliza, reaseguro |
| 89 | Comercio | retail, venta, consumo, supermercado, inflacion de precios |
| 99 | Congreso | diputados, senado, proyecto de ley, sesion |
| 337 | Destacados | Asignacion manual (toggle en el bot) |
| 239 | Digitalizacion Pymes | tecnologia, ecommerce, startup, fintech, ia |
| 94 | Economia | inflacion, dolar, bcra, pbi, cepo, deuda, fmi |
| 96 | Empresas | pyme, negocio, emprendimiento, ceo, inversion |
| 100 | Gobierno | ministerio, presidencia, decreto, obra publica |
| 90 | Industria | manufactura, automotriz, textil, acero |
| 103 | Informes | encuesta, estadistica, indec, ipc, emae |
| 97 | Internacional | mundial, exportacion, china, eeuu, mercosur |
| 98 | Nacional | argentina, pais (fallback si no hay match) |
| 91 | Opinion | analisis, columna, editorial |
| 101 | Poder Judicial | juicio, tribunal, corte suprema, fallo |
| 87 | Politica | elecciones, partido, peronismo, oposicion |
| 102 | Provincias | provincial, municipal, gobernacion |
| 92 | Servicios | salud, educacion, energia, tarifas |
| 93 | Sindicatos | gremio, paritaria, salario, huelga, cgt |

---

## 5. Etiquetas (Tags)

- Hasta **8 etiquetas** por nota.
- Se extraen del titulo + primer parrafo del contenido.
- Minimo 4 caracteres, sin stop-words.
- Cada etiqueta va en **Title Case** (primera letra mayuscula).
- Sin duplicados.

---

## 6. Imagenes

- **Imagen destacada:** Siempre que la fuente la provea (og:image).
- **Alt text:** `"{Keyword} - {Titulo SEO}"`. Ejemplo: *"Inflacion - AFIP extiende plazo monotributistas"*.
- **Formato preferido:** JPG/WebP, ratio 16:9.

---

## 7. Redes sociales

### 7.1 Twitter / X

| Elemento | Regla |
|----------|-------|
| Largo total | **280 caracteres** max |
| Formato | Titulo + URL + Hashtags |
| Titulo | Usar titulo SEO (60 chars max) |
| Hashtags | Hasta 3 del titulo + #Pymes fijo |
| URL | Link al post en WordPress |

Estructura:
```
{Titulo}

{URL WordPress}

#{Tag1} #{Tag2} #{Tag3} #Pymes
```

### 7.2 Canal de Telegram (@EmpresarialARG)

Estructura:
```
📰 **{Titulo}**

{Excerpt, 200 chars max}

🔗 Leer nota completa: {URL}
```

Con imagen destacada cuando este disponible.

### 7.3 WhatsApp (copy-paste manual)

Estructura:
```
📰 {Titulo}

{Excerpt, 200 chars max}

🔗 {URL}
```

---

## 8. Limpieza de contenido scrapeado

El bot automaticamente elimina del texto:

- Fragmentos de UI: "compartir en", "seguinos", "suscribite", etc.
- Secciones de comentarios
- Mensajes de error de browser/HTML5
- Lineas con encoding roto (caracteres Ã, Â, â€, etc.)
- Lineas menores a 25 caracteres que no terminan en puntuacion

---

## 9. Checklist de publicacion

Antes de publicar, verificar:

- [ ] Titulo <= 60 caracteres, keyword al inicio
- [ ] Slug <= 50 caracteres, sin tildes
- [ ] Meta description 120-155 caracteres con keyword
- [ ] Lead en negrita, max 280 caracteres
- [ ] H2 cada 2-4 parrafos, primer H2 con keyword
- [ ] Subtitulos H2 descriptivos (no genericos)
- [ ] Parrafos cortos (max 80 palabras)
- [ ] Datos y cifras en negrita
- [ ] Citas en parrafo propio con atribucion
- [ ] Recuadro "Resumen para Pymes" presente
- [ ] Fuente con link al original
- [ ] Imagen con alt text SEO
- [ ] Categorias correctas (max 3)
- [ ] Etiquetas relevantes (max 8)
