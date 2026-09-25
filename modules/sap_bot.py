                # apaga obra atual -> digita a proxima -> ENTER.
                self._return_to_initial_and_load_next(
                    current_obra=obra,
                    next_obra=next_obra,
                )

            except pyautogui.FailSafeException as exc:
                self._error_screenshot(obra, "FAILSAFE")
                results_by_obra[obra] = {
                    "obra": obra,
                    "ok": False,
                    "error": "Automacao interrompida pelo usuario (FAILSAFE).",
                    "records": [],
                }
                self.emit(
                    f"Obra {obra}: execucao interrompida pelo FAILSAFE.",
                    level="error",
                )
                break
            except Exception as exc:
                self._error_screenshot(obra, "COLETA_SAP")
                self.emit(f"Obra {obra}: ERRO na coleta SAP - {exc}", level="error")
                results_by_obra[obra] = {
                    "obra": obra,
                    "ok": False,
                    "error": str(exc),
                    "records": [],
                }

                # Tenta voltar para a tela inicial e carregar a proxima obra
                # mesmo quando a coleta atual falhar, para nao travar a lista.
                try:
                    self._return_to_initial_and_load_next(obra, next_obra)
                except Exception as nav_exc:
                    self.emit(
                        f"Obra {obra}: nao foi possivel preparar a proxima obra: {nav_exc}",
                        level="error",
                    )
                    break

        # ------------------------------------------------------------
        # FASE 2: PROCESSAMENTO LOCAL DOS TXTs JA COLETADOS
        # ------------------------------------------------------------
        if remote_jobs:
            self.emit(
                "Coleta no SAP concluida. Iniciando processamento das fotos no navegador local...",
                level="success",
                progress=0.34,
            )

        for local_idx, job in enumerate(remote_jobs):
            obra = job["obra"]
            try:
                start = 0.34 + (0.64 * local_idx / max(1, len(remote_jobs)))
                end = 0.34 + (0.64 * (local_idx + 1) / max(1, len(remote_jobs)))
                result = self._process_local_links_for_obra(
                    obra=obra,
                    obra_dir=job["obra_dir"],
                    txt_path=job["txt_path"],
                    all_urls=job["urls"],
                    progress_start=start,
                    progress_end=min(0.99, end),
                )
                results_by_obra[obra] = result
            except Exception as exc:
                self.emit(f"Obra {obra}: ERRO no processamento local - {exc}", level="error")
                results_by_obra[obra] = {
                    "obra": obra,
                    "ok": False,
                    "error": str(exc),
                    "records": [],
                }

        # Monta um unico Word consolidado, com uma obra por pagina.
        # O Word substitui os antigos arquivos Excel de coordenadas.
        ordered_results = [
            results_by_obra.get(obra, {
                "obra": obra,
                "ok": False,
                "error": "Obra nao processada.",
                "records": [],
            })
            for obra in normalized
        ]

        successful = [r for r in ordered_results if r.get("ok")]
        if successful:
            try:
                report_path = default_report_path(self.output_root)
                word_path, base_used = build_word_report(successful, report_path)
                for r in successful:
                    r["word"] = word_path
                    r["base_word"] = base_used
                self.emit(
                    f"Relatorio Word concluido: {word_path.name} "
                    f"({len(successful)} obra(s), uma por pagina).",
                    level="success",
                    progress=1.0,
                )
                if base_used:
                    self.emit(
                        f"Base utilizada no Word: {Path(base_used).name}.",
                        level="info",
                    )
                else:
                    self.emit(
                        "Base de levantamento nao encontrada. O Word foi gerado "
                        "com os campos adicionais em branco.",
                        level="warning",
                    )
            except Exception as exc:
                self.emit(
                    f"Falha ao gerar relatorio Word: {exc}",
                    level="error",
                )
                for r in successful:
                    r["word"] = None
                    r["word_error"] = str(exc)

        # Mantem exatamente a mesma ordem digitada/colada na ferramenta.
        return ordered_results
