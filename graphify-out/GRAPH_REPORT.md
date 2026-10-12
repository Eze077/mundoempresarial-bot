# Graph Report - mundoempresarial-bot  (2026-10-12)

## Corpus Check
- 7 files · ~86,465 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 11 file(s) not represented in the graph (top: (none) 4, .ttf 4, .example 1)

## Summary
- 786 nodes · 1704 edges · 60 communities (44 shown, 16 thin omitted)
- Extraction: 85% EXTRACTED · 15% INFERRED · 0% AMBIGUOUS · INFERRED: 258 edges (avg confidence: 0.84)
- Token cost: 155,159 input · 0 output

## Community Hubs (Navigation)
- Placas y frases
- Manual de estilo y SEO
- Publicador /pubx
- Nota desde tweet
- Imports y núcleo de main.py
- Comandos del bot
- Alertas de créditos
- GA4 e imágenes de tweets
- Botones del harness
- Fuentes y feedback
- Nota manual / columnas
- Ecos y encuestas (teclados)
- Comandos admin
- /evento y adjuntos
- /vivo: canales y cierre
- Formato de nota y ToC
- /vivo: arranque
- Publicación en WordPress
- Efemérides
- Edición y difusión
- /editar y /publicador
- Frases programadas
- Preview, hashtags y categorías
- YouTube y transcripción
- Encuestas: publicar y enviar
- main · _do_schedule
- main · md_escape
- main · cmd_pipeline
- main · _transcript_via_ytdlp
- main · cmd_hilo
- main · e
- main · cmd_editor
- main · cmd_livingnotes
- main · _reel_armar
- main · focus_keyword
- main · _ln_armar
- frases_base · Plantilla de frase destacada
- main · _process_url_for_review
- main · cmd_creditos
- main · _ln_dictamen
- main · _wp_publish_frase
- main · _handle_vvb_button
- main · get_excerpt
- main · _format_frase_content
- main · get_title
- main · handle_iev_button
- main · on_finde_approval_cb
- main · _xcola_callback
- main · cmd_eventos
- main · cmd_notamanual
- main · cmd_testtwitter
- main · _borrar_cards_briefing
- main · detect_content_type
- main · _gpt_html_to_gutenberg
- main · _pipeline_stage_detail
- main · _publicar_nota_manual_direct
- main · _wrap_nota_desplegable
- main · _title_keywords
- main · _target_datetime_for_slot
- main · tweet_id_from_url

## God Nodes (most connected - your core abstractions)
1. `handle_button()` - 98 edges
2. `main()` - 65 edges
3. `handle_link()` - 59 edges
4. `handle_edito_button()` - 34 edges
5. `e()` - 31 edges
6. `handle_vv_button()` - 25 edges
7. `handle_edit_button()` - 21 edges
8. `wp_auth()` - 20 edges
9. `handle_pubx_button()` - 19 edges
10. `openai_post()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `_frase_ig_wa()` --indirect_call--> `pad_placa()`  [INFERRED]
  main.py → frases_gen.py
- `cmd_frases()` --indirect_call--> `generate_frase_image()`  [INFERRED]
  main.py → frases_gen.py
- `_do_frase_schedule()` --indirect_call--> `generate_frase_image()`  [INFERRED]
  main.py → frases_gen.py
- `_fire_frase_social()` --indirect_call--> `generate_frase_image()`  [INFERRED]
  main.py → frases_gen.py
- `handle_link()` --indirect_call--> `generate_frase_image()`  [INFERRED]
  main.py → frases_gen.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Colocacion del keyword de enfoque para puntaje Rank Math** — manual_de_estilo_keyword_de_enfoque, manual_de_estilo_titulo_seo, manual_de_estilo_url_slug, manual_de_estilo_bajada_lead, manual_de_estilo_subtitulos_h2, manual_de_estilo_meta_description, manual_de_estilo_imagenes_y_alt_text, manual_de_estilo_rank_math_puntaje [EXTRACTED 1.00]
- **Distribucion de la nota en redes (Twitter, Telegram, WhatsApp)** — manual_de_estilo_twitter_x_formato, manual_de_estilo_canal_telegram_formato, manual_de_estilo_whatsapp_formato [EXTRACTED 1.00]
- **Stack de ingesta y scraping de contenido** — requirements_trafilatura, requirements_beautifulsoup4, requirements_feedparser, requirements_googlenewsdecoder, requirements_youtube_transcript_api, requirements_yt_dlp, requirements_pypdf [INFERRED 0.75]

## Communities (60 total, 16 thin omitted)

### Community 0 - "Placas y frases"
Cohesion: 0.08
Nodes (21): generate_frase_image(), _load_font(), pad_placa(), _replace_header(), _wrap(), _chip(), _draw_title(), ensure_fonts() (+13 more)

### Community 1 - "Manual de estilo y SEO"
Cohesion: 0.07
Nodes (44): Manual de Estilo y SEO - MundoEmpresarial.ar, Bajada / Lead en negrita (max 280 chars, con keyword), Boton Desplegable ON/OFF del bot (segundo POST a WP API), Campos meta Rank Math en WordPress (rank_math_title, description, focus_keyword, robots, og_content_image), Formato Canal de Telegram @MundoEmpresarial_AR, Categorias (hasta 3, titulo con triple peso), Checklist de publicacion, Citas y declaraciones en parrafo propio con atribucion (+36 more)

### Community 2 - "Publicador /pubx"
Cohesion: 0.05
Nodes (27): _build_pubx_del_kb(), _build_pubx_hora_kb(), _build_pubx_links_kb(), _build_pubx_rep_kb(), build_tweet(), delete_from_channel(), delete_linkedin_post(), delete_tweet() (+19 more)

### Community 3 - "Nota desde tweet"
Cohesion: 0.05
Nodes (26): _clean_tweet_text(), _crear_nota_tweet(), detect_url_kind(), _dup_manual_hl(), _ed_kb(), _ed_mod(), _edicion_callback(), _mostrar() (+18 more)

### Community 4 - "Imports y núcleo de main.py"
Cohesion: 0.08
Nodes (24): _edito_chpadre_kb(), _edito_count_subtemas(), _edito_disc_kb(), _edito_disc_text(), _edito_get_living_notes(), _edito_get_notas_tema(), _edito_get_subtemas(), _edito_get_tema() (+16 more)

### Community 5 - "Comandos del bot"
Cohesion: 0.08
Nodes (21): _build_commands_text(), cmd_briefing(), cmd_ciclaje(), cmd_coladepublicacion(), cmd_comandos(), cmd_desbloquear(), cmd_encuesta(), cmd_kwtemp() (+13 more)

### Community 6 - "Alertas de créditos"
Cohesion: 0.06
Nodes (17): alert_admin_sync(), alert_admin_throttled(), _alert_twitter_billing(), _check_credits_status(), _check_openai_billing(), clean_text(), _decode_google_news_path(), _detect_media() (+9 more)

### Community 7 - "GA4 e imágenes de tweets"
Cohesion: 0.07
Nodes (16): _describe_tweet_image(), _doc_parrafos(), _fit_tweet(), _ga4_best_slots(), _ga4_fetch_data(), _ga4_weekly_report(), generate_thread_with_gpt(), _gpt_nota_manual_extract() (+8 more)

### Community 9 - "Fuentes y feedback"
Cohesion: 0.09
Nodes (16): _add_scheduled_job(), _add_source(), _default_feedback(), _delete_source(), _domain_of(), _find_or_create_feedback_post(), find_source(), _fire_scheduled_social() (+8 more)

### Community 10 - "Nota manual / columnas"
Cohesion: 0.09
Nodes (15): _build_nota_manual_data(), _chunks(), _cols_det_html(), _cols_estructura_verbatim(), _crear_job_nota_manual(), _doc_sin_titulo_ni_firma(), _enqueue_evento(), _format_columna_html() (+7 more)

### Community 11 - "Ecos y encuestas (teclados)"
Cohesion: 0.10
Nodes (17): _build_eco_sched_day_kb(), _build_eco_sched_hour_kb(), _build_eco_schedule_kb(), _build_enc_kb(), _build_frase_sched_day_kb(), _build_frase_sched_hour_kb(), _build_frase_sched_pre_ht_kb(), _build_frase_schedule_kb() (+9 more)

### Community 12 - "Comandos admin"
Cohesion: 0.10
Nodes (11): _admin_ids(), cmd_campania(), cmd_ingesta(), cmd_inst(), cmd_nutricion(), cmd_resume(), cmd_stop(), main() (+3 more)

### Community 13 - "/evento y adjuntos"
Cohesion: 0.12
Nodes (14): cmd_evento(), cmd_input_evento(), _evento_kb(), handle_document(), handle_evento_media(), handle_photo_message(), _iev_add_text(), _iev_foto() (+6 more)

### Community 14 - "/vivo: canales y cierre"
Cohesion: 0.14
Nodes (19): handle_vv_button(), _vv_canales_activo(), _vv_canales_kb(), _vv_cierre_go(), _vv_cierre_kb(), _vv_cmd_mail(), _vv_dif_go(), _vv_dif_kb() (+11 more)

### Community 15 - "Formato de nota y ToC"
Cohesion: 0.13
Nodes (8): _build_rank_math_toc(), format_content(), _append_h2(), _generate_h2(), _gpt_format_article(), _is_valid_h2(), normalize_text(), _slugify_h2()

### Community 16 - "/vivo: arranque"
Cohesion: 0.18
Nodes (12): cmd_vivo(), _vv_arrancar(), _vv_canales_default(), _vv_capturar_foto(), _vv_capturar_texto(), _vv_cierre_texto(), _vv_cmd_cerrar(), _vv_cmd_difundir() (+4 more)

### Community 17 - "Publicación en WordPress"
Cohesion: 0.20
Nodes (8): append_social_meta(), _apply_wm_to_featured(), _foto_rehacer(), get_or_create_tags(), publish_post(), update_post(), upload_image_bytes(), wp_auth()

### Community 18 - "Efemérides"
Cohesion: 0.20
Nodes (6): _efem_espera(), _efem_kb(), _efem_panel_text(), _efem_redraw(), _save(), handle_efem_button()

### Community 19 - "Edición y difusión"
Cohesion: 0.20
Nodes (5): _build_category_kb(), _build_delete_kb(), _build_publish_social_kb(), handle_edit_button(), _post_to_data()

### Community 20 - "/editar y /publicador"
Cohesion: 0.20
Nodes (6): _build_edit_kb(), _build_pubx_main_kb(), cmd_borrar(), cmd_editar(), cmd_publicador(), find_post()

### Community 21 - "Frases programadas"
Cohesion: 0.20
Nodes (6): _build_frase_kb(), _lbl(), _do_frase_schedule(), find_scheduled_collision(), _frase_edit(), _frase_flag()

### Community 22 - "Preview, hashtags y categorías"
Cohesion: 0.25
Nodes (6): _build_hashtags(), build_preview(), detect_categories(), _enqueue_to_harness(), extract_tags(), _load_ht_mapping()

### Community 23 - "YouTube y transcripción"
Cohesion: 0.22
Nodes (5): _clean_transcript(), scrape_youtube(), _summarize_transcript(), _transcript_via_whisper(), youtube_video_id()

### Community 24 - "Encuestas: publicar y enviar"
Cohesion: 0.22
Nodes (4): _do_enc_newsletter(), _do_encuesta_publish(), _run(), _enc_wa_delivery()

### Community 25 - "main · _do_schedule"
Cohesion: 0.25
Nodes (5): _build_eco_kb(), _do_eco_schedule(), _do_schedule(), _eco_data_with_alt(), _eco_preview_text()

### Community 26 - "main · md_escape"
Cohesion: 0.25
Nodes (4): cmd_frases(), cmd_fuentes(), _learn_hashtags_daily(), md_escape()

### Community 27 - "main · cmd_pipeline"
Cohesion: 0.25
Nodes (3): cmd_pipeline(), _pipeline_kb(), _pipeline_stats()

### Community 28 - "main · _transcript_via_ytdlp"
Cohesion: 0.25
Nodes (4): _parse_json3(), _parse_vtt(), _transcript_via_html(), _transcript_via_ytdlp()

### Community 29 - "main · cmd_hilo"
Cohesion: 0.29
Nodes (4): _build_thread_preview(), cmd_hilo(), handle_thread_button(), post_twitter_thread()

### Community 30 - "main · e"
Cohesion: 0.29
Nodes (4): _chequeo_rapido(), e(), _foto_guardar_original(), _summarize_with_gpt()

### Community 31 - "main · cmd_editor"
Cohesion: 0.29
Nodes (5): cmd_editor(), _edito_get_descubrimientos(), _edito_get_temas(), _edito_main_kb(), _edito_main_text()

### Community 32 - "main · cmd_livingnotes"
Cohesion: 0.29
Nodes (4): cmd_livingnotes(), _ln_catalogo(), _ln_grupo(), _ln_menu_cats()

### Community 33 - "main · _reel_armar"
Cohesion: 0.29
Nodes (4): cmd_reel(), _reel_armar(), _reel_kb(), _reel_mod()

### Community 34 - "main · focus_keyword"
Cohesion: 0.33
Nodes (4): focus_keyword(), _handle_edit_photo_bytes(), _publish_processed(), publish_to_channel()

### Community 35 - "main · _ln_armar"
Cohesion: 0.29
Nodes (4): _h_esc(), _lb_mod(), _ln_armar(), _lnb_kb()

### Community 36 - "frases_base · Plantilla de frase destacada"
Cohesion: 0.40
Nodes (6): Barra de redes sociales (IG, FB, X, YouTube, LinkedIn, WhatsApp, Telegram, web), Frase destacada (formato de placa), Categoria Inspiracion, MundoEmpresarial.AR - La Voz de las Pymes, Paleta marca ME (navy, celeste, naranja), Plantilla de frase destacada (frases_base.png)

### Community 37 - "main · _process_url_for_review"
Cohesion: 0.33
Nodes (3): _auto_publish_url(), detect_hilo(), _process_url_for_review()

### Community 38 - "main · cmd_creditos"
Cohesion: 0.33
Nodes (3): cmd_creditos(), _format_credits_report(), send_weekly_credits_check()

### Community 39 - "main · _ln_dictamen"
Cohesion: 0.33
Nodes (3): _comite_mod(), _ln_dictamen(), _ln_kb()

### Community 40 - "main · _wp_publish_frase"
Cohesion: 0.33
Nodes (3): _gpt_frase_body(), _meta_safe(), _wp_publish_frase()

### Community 41 - "main · _handle_vvb_button"
Cohesion: 0.53
Nodes (5): _handle_vvb_button(), _vv_edit(), _vvb_archivar_tanda(), _vvb_kb(), _vvb_publicar()

### Community 42 - "main · get_excerpt"
Cohesion: 0.40
Nodes (3): get_excerpt(), meta_description(), rewrite_excerpt_with_gpt()

### Community 43 - "main · _format_frase_content"
Cohesion: 0.50
Nodes (3): _format_frase_content(), pyme_box(), pyme_summary()

### Community 45 - "main · handle_iev_button"
Cohesion: 0.50
Nodes (3): handle_iev_button(), _vvb_ajustar(), _vvb_mostrar_preview()

## Knowledge Gaps
- **12 isolated node(s):** `Citas y declaraciones en parrafo propio con atribucion`, `Datos y cifras en negrita`, `Densidad del keyword (1-2,5% optima)`, `URL / Slug (keyword, <=75 chars)`, `Largo del contenido (minimo 600 palabras)` (+7 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 331 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **16 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `handle_button()` connect `Botones del harness` to `Publicador /pubx`, `Nota desde tweet`, `Imports y núcleo de main.py`, `Comandos del bot`, `Ecos y encuestas (teclados)`, `Comandos admin`, `Publicación en WordPress`, `Efemérides`, `Frases programadas`, `Preview, hashtags y categorías`, `Encuestas: publicar y enviar`, `main · _do_schedule`, `main · md_escape`, `main · cmd_pipeline`, `main · e`, `main · cmd_livingnotes`, `main · _reel_armar`, `main · focus_keyword`, `main · _ln_armar`, `main · _ln_dictamen`, `main · get_excerpt`, `main · get_title`, `main · _xcola_callback`, `main · _borrar_cards_briefing`, `main · _pipeline_stage_detail`, `main · _publicar_nota_manual_direct`, `main · _target_datetime_for_slot`, `main · tweet_id_from_url`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Are the 33 inferred relationships involving `handle_button()` (e.g. with `append_social_meta()` and `_chequeo_rapido()`) actually correct?**
  _`handle_button()` has 33 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Citas y declaraciones en parrafo propio con atribucion`, `Datos y cifras en negrita`, `Densidad del keyword (1-2,5% optima)` to the rest of the system?**
  _12 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Placas y frases` be split into smaller, more focused modules?**
  _Cohesion score 0.08233117483811286 - nodes in this community are weakly interconnected._
- **Why does `_f()` connect `Placas y frases` to `Comandos del bot`, `Preview, hashtags y categorías`?**
  _High betweenness centrality (0.022) - this node is a cross-community bridge._
- **Are the 61 inferred relationships involving `main()` (e.g. with `cmd_borrar()` and `cmd_briefing()`) actually correct?**
  _`main()` has 61 INFERRED edges - model-reasoned connections that need verification._
- **Should `Manual de estilo y SEO` be split into smaller, more focused modules?**
  _Cohesion score 0.06567992599444958 - nodes in this community are weakly interconnected._