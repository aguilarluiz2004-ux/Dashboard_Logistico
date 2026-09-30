"""
Monitora a planilha no seu PC e envia automaticamente para o PythonAnywhere
quando o arquivo for alterado.

Instalação:
    python -m pip install requests

Execução:
    python enviar_planilha.py

Deixe esta janela aberta enquanto quiser o monitoramento automático.
"""

import os
import shutil
import tempfile
import time

import requests


# ============================================================
# CONFIGURAÇÕES
# ============================================================

CAMINHO_PLANILHA_PC = (
    r"C:\Users\LuizAugustoAguilarTo"
    r"\OneDrive - 4log"
    r"\Área de Trabalho"
    r"\Dashboard"
    r"\base"
    r"\base_operacao.xlsx"
)

URL = (
    "https://LuizAguilartorres.pythonanywhere.com"
    "/api/upload-planilha"
)

TOKEN = os.environ.get(
    "UPLOAD_TOKEN",
    "dmfSZOG5_BgI8Gcr9GlIIw9YJj_M9Ozfeh3uvXPlYnA"
)

INTERVALO = 10
ESTABILIZAR = 5


# ============================================================
# COPIAR PLANILHA PARA TEMPORÁRIO
# ============================================================

def copiar_para_temporario():
    destino = os.path.join(
        tempfile.gettempdir(),
        "base_operacao_envio.xlsx"
    )

    for tentativa in range(1, 6):
        try:
            shutil.copy2(
                CAMINHO_PLANILHA_PC,
                destino
            )
            return destino

        except PermissionError:
            print(
                f"Arquivo ocupado. Tentativa {tentativa}/5..."
            )
            time.sleep(2)

    raise PermissionError(
        "Arquivo ocupado. Não foi possível criar a cópia temporária."
    )


# ============================================================
# ENVIAR PLANILHA
# ============================================================

def enviar():
    copia = None

    try:
        copia = copiar_para_temporario()

        with open(copia, "rb") as arquivo:
            resposta = requests.post(
                URL,
                headers={
                    "X-Token": TOKEN
                },
                files={
                    "planilha": (
                        "base_operacao.xlsx",
                        arquivo,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                },
                timeout=180
            )

        horario = time.strftime("%H:%M:%S")

        if resposta.ok:
            print(
                f"[{horario}] "
                "Planilha enviada e importada com sucesso."
            )

            try:
                print(f"Servidor: {resposta.json()}")
            except ValueError:
                pass

            return True

        print("=" * 70)
        print(f"[{horario}] FALHA NO SERVIDOR")
        print(f"Status HTTP: {resposta.status_code}")
        print("Resposta do servidor:")

        try:
            print(resposta.json())
        except ValueError:
            print(resposta.text)

        print("=" * 70)

        return False

    except requests.exceptions.Timeout:
        print(
            f"[{time.strftime('%H:%M:%S')}] "
            "Tempo limite excedido ao enviar a planilha."
        )
        return False

    except requests.exceptions.ConnectionError as erro:
        print(
            f"[{time.strftime('%H:%M:%S')}] "
            "Não foi possível conectar ao PythonAnywhere."
        )
        print(f"Detalhes: {erro}")
        return False

    except Exception as erro:
        print(
            f"[{time.strftime('%H:%M:%S')}] "
            f"Erro ao enviar: {type(erro).__name__}: {erro}"
        )
        return False

    finally:
        if copia and os.path.exists(copia):
            try:
                os.remove(copia)
            except OSError:
                pass


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    ultima = None

    print("=" * 70)
    print("MONITOR DE PLANILHA")
    print("=" * 70)
    print(f"Vigiando:\n{CAMINHO_PLANILHA_PC}")
    print(f"Servidor:\n{URL}")
    print(f"Intervalo: {INTERVALO} segundos")
    print(f"Estabilização: {ESTABILIZAR} segundos")
    print("=" * 70)

    while True:
        try:
            atual = os.path.getmtime(
                CAMINHO_PLANILHA_PC
            )

            if atual != ultima:
                print(
                    f"[{time.strftime('%H:%M:%S')}] "
                    f"Planilha alterada. "
                    f"Aguardando {ESTABILIZAR} segundos..."
                )

                time.sleep(ESTABILIZAR)

                novo_mtime = os.path.getmtime(
                    CAMINHO_PLANILHA_PC
                )

                if novo_mtime != atual:
                    print(
                        f"[{time.strftime('%H:%M:%S')}] "
                        "A planilha ainda está sendo modificada. "
                        "Vou verificar novamente."
                    )
                    time.sleep(INTERVALO)
                    continue

                print(
                    f"[{time.strftime('%H:%M:%S')}] "
                    "Enviando planilha..."
                )

                if enviar():
                    ultima = novo_mtime

            time.sleep(INTERVALO)

        except FileNotFoundError:
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                "Planilha não encontrada."
            )
            print(
                f"Confira o caminho:\n{CAMINHO_PLANILHA_PC}"
            )
            time.sleep(INTERVALO)

        except PermissionError as erro:
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Arquivo sem acesso: {erro}"
            )
            time.sleep(INTERVALO)

        except Exception as erro:
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Erro no monitor: {type(erro).__name__}: {erro}"
            )
            time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
