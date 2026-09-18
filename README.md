# Robô SAP — Fotos e Coordenadas

Automação local em Python/Streamlit para operar uma sessão RDP/SAP já aberta, navegar até **Dados de Campo 2 → Imagens de Campo**, localizar fotos de interesse, abrir a imagem, extrair latitude/longitude do carimbo por OCR, salvar os recortes e gerar Excel por obra.

## Versão EXE sem instalar Tesseract no notebook

Esta pasta agora inclui build para Windows com **Python + bibliotecas + Tesseract OCR embutidos**.

Leia primeiro:

- `README_EXE.md`
- `INSTRUCOES_RAPIDAS_EXE.txt`

O método recomendado é usar o workflow:

```text
.github/workflows/build-windows-exe.yml
```

Ele gera automaticamente em um runner Windows:

```text
SAP_Fotos.exe
SAP_Fotos_Portatil.zip
SHA256.txt
```

Assim, o notebook de destino não precisa instalar Python ou Tesseract.

> Se a empresa bloquear executáveis não assinados/portáteis, a liberação deve ser tratada com a TI. O projeto não contorna controles corporativos.

## Fluxo automatizado

Para cada obra:

1. ativa a Área Remota já aberta;
2. preenche o campo `Nota`;
3. pressiona Enter;
4. abre `Dados de Campo 2`;
5. abre `Imagens de Campo`;
6. procura por OCR:
   - `FACHADAIMOVEL` / `FACHADADOIMOVEL`;
   - `ADESIVOLIGACAONOVA`;
   - `FOTOPANORAMICA`;
7. abre cada link;
8. trata o popup `Segurança SAPGUI → Permitir`;
9. maximiza a foto;
10. recorta a área da fotografia;
11. lê o rodapé por OCR;
12. extrai latitude e longitude;
13. salva JPG;
14. gera Excel e ZIP por obra;
15. atualiza `resumo_geral.xlsx`.

## Dados da versão empacotada

Quando executado como EXE, configuração, logs, debug e saídas ficam em:

```text
%LOCALAPPDATA%\SAP_Fotos\
```

Use o botão **Abrir pasta de saída** na interface.

## Desenvolvimento em Python

A versão-fonte continua podendo ser executada normalmente com:

```text
install.bat
run.bat
```

Nesse modo de desenvolvimento, pode ser necessário instalar/configurar o Tesseract separadamente. Para o notebook corporativo, prefira o build EXE descrito em `README_EXE.md`.
