# SAP Fotos - versão EXE sem instalação do Tesseract

Esta edição foi preparada para gerar um executável Windows que já leva dentro dele:

- Python;
- Streamlit;
- Pandas / OpenPyXL;
- OpenCV;
- PyAutoGUI / PyGetWindow;
- RapidFuzz;
- Pytesseract;
- o executável do Tesseract OCR e o `tessdata` necessário.

## O notebook da empresa precisa instalar alguma coisa?

**Não para executar o pacote gerado.**

A opção recomendada é gerar o binário em uma máquina de build externa (por exemplo, GitHub Actions) e depois copiar somente o artefato permitido para o notebook da empresa.

O aplicativo continua sujeito às políticas de segurança da empresa. Se Windows Defender, SmartScreen, AppLocker ou outra política bloquear executáveis não assinados, a solução correta é solicitar liberação à TI; este projeto não tenta contornar essas proteções.

## Dois formatos gerados

### 1. `SAP_Fotos.exe`

Arquivo único. Ao abrir, ele extrai os componentes internos temporariamente e inicia o Streamlit no navegador local.

Vantagens:
- um único arquivo para transportar;
- nenhuma instalação de Python/Tesseract.

Desvantagens:
- inicialização mais lenta;
- executáveis `one-file` podem receber mais alertas de antivírus corporativo.

### 2. `SAP_Fotos_Portatil.zip`

Versão recomendada para ambiente corporativo quando a TI permite executar aplicações portáteis. Basta extrair a pasta e executar `SAP_Fotos_Portatil.exe`.

Vantagens:
- inicializa mais rápido;
- costuma ser mais fácil de analisar/liberar pela TI;
- Tesseract fica na subpasta `tesseract`, sem instalação.

## Onde ficam configuração, logs e resultados

Quando executado como EXE, o programa grava dados do usuário em:

```text
%LOCALAPPDATA%\SAP_Fotos\
```

Estrutura típica:

```text
SAP_Fotos\
├── config.json
├── debug\
├── logs\
└── saida\
    ├── 1114882266\
    │   ├── 1114882266.jpg
    │   ├── 1114882266_FACHADAIMOVEL.jpg
    │   ├── 1114882266_ADESIVOLIGACAONOVA.jpg
    │   ├── 1114882266_FOTOPANORAMICA.jpg
    │   └── 1114882266_coordenadas.xlsx
    └── resumo_geral.xlsx
```

A própria interface tem o botão **Abrir pasta de saída**.

---

# Como gerar o EXE sem instalar nada no notebook da empresa

## Método recomendado: GitHub Actions

O projeto inclui:

```text
.github/workflows/build-windows-exe.yml
```

Esse workflow usa um Windows temporário do GitHub para:

1. instalar Python apenas no runner de build;
2. instalar as bibliotecas;
3. instalar Tesseract apenas no runner;
4. copiar o Tesseract para dentro do pacote;
5. gerar `SAP_Fotos.exe`;
6. gerar `SAP_Fotos_Portatil.zip`;
7. gerar hashes SHA-256;
8. disponibilizar tudo como artefato do workflow.

### Passos

1. Coloque esta pasta em um repositório GitHub privado autorizado pela empresa.
2. Abra a aba **Actions**.
3. Selecione **Build Windows EXE**.
4. Clique em **Run workflow**.
5. Aguarde o job terminar.
6. Baixe o artefato **SAP-Fotos-Windows**.

Ele conterá:

```text
SAP_Fotos.exe
SAP_Fotos_Portatil.zip
SHA256.txt
```

Nenhuma instalação é feita no notebook onde o robô será usado.

---

# Build em outro computador Windows

Se houver um computador Windows autorizado para desenvolvimento, também existe:

```text
build_exe_windows.bat
```

Nesse caso, coloque uma distribuição Tesseract Windows completa em:

```text
vendor\tesseract\
```

contendo pelo menos:

```text
vendor\tesseract\tesseract.exe
vendor\tesseract\tessdata\eng.traineddata
```

Depois execute `build_exe_windows.bat`.

---

# Uso no notebook

1. Abra a Área de Trabalho Remota.
2. Faça login normalmente.
3. Deixe o SAP aberto na tela esperada pelo robô.
4. Execute `SAP_Fotos.exe` ou `SAP_Fotos_Portatil.exe`.
5. O navegador abrirá automaticamente em `127.0.0.1` numa porta local disponível.
6. Faça o diagnóstico e a calibração antes do primeiro processamento.
7. Execute primeiro uma única obra de teste.

## Segurança operacional

Durante a automação, não use mouse/teclado na mesma sessão. O PyAutoGUI está com `FAILSAFE` ativo: mover o ponteiro rapidamente para o canto superior esquerdo interrompe a automação.
