"""
Vigia a planilha no seu PC e envia para o PythonAnywhere quando ela mudar.

No seu computador:

    python -m pip install requests
    python enviar_planilha.py

Deixe a janela aberta para continuar monitorando a planilha.
"""

import os
import shutil
import tempfile
import time
import requests


# ============================================================
# CONFIGURAÇÕES
# ============================================================

# Caminho REAL da sua planilha no computador
CAMINHO_PLANILHA_PC = (
    r"C:\Users\LuizAugustoAguilarTo"
    r"\OneDrive - 4log"
    r"\Área de Trabalho"
    r"\Dashboard"
    r"\base"
    r"\base_operacao.xlsx"
)

# Endereço da API hospedada no PythonAnywhere
URL = (
    "https://LuizAguilartorres.pythonanywhere.com"
    "/api/upload-planilha"
)

# Token de segurança
TOKEN = os.environ.get(
    "UPLOAD_TOKEN",
    "dmfSZOG5_BgI8Gcr9GlIIw9YJj_M9Ozfeh3uvXPlYnA"
)

# Tempo entre cada verificação
INTERVALO = 10

# Tempo para garantir que Excel/OneDrive terminou de salvar
ESTABILIZAR = 5


# ============================================================
# COPIAR PLANILHA PARA ARQUIVO TEMPORÁRIO
# ============================================================

def copiar_para_temporario():
    """
    Copia a planilha para um arquivo temporário.

    Isso evita tentar enviar diretamente um arquivo
    que esteja aberto pelo Excel.
    """

    destino = os.path.join(
        tempfile.gettempdir(),
        "base_operacao_envio.xlsx"
    )

    for tentativa in range(5):

        try:

            shutil.copy2(
                CAMINHO_PLANILHA_PC,
                destino
            )

            return destino

        except PermissionError:

            print(
                f"Arquivo ocupado. "
                f"Tentativa {tentativa + 1}/5..."
            )

            time.sleep(2)

    raise PermissionError(
        "Arquivo ocupado, não consegui copiar."
    )


# ============================================================
# ENVIAR PLANILHA
# ============================================================

def enviar():

    try:

        copia = copiar_para_temporario()

        with open(copia, "rb") as f:

            resposta = requests.post(

                URL,

                headers={
                    "X-Token": TOKEN
                },

                files={
                    "planilha": (
                        "base_operacao.xlsx",
                        f
                    )
                },

                timeout=180
            )

        # ====================================================
        # SUCESSO
        # ====================================================

        if resposta.ok:

            print(
                f"[{time.strftime('%H:%M:%S')}] "
                "Planilha enviada e importada com sucesso."
            )

            return True

        # ====================================================
        # ERRO DA API
        # ====================================================

        print(
            f"[{time.strftime('%H:%M:%S')}] "
            f"Falha ({resposta.status_code}): "
            f"{resposta.text}"
        )

        return False

    except requests.exceptions.Timeout:

        print(
            f"[{time.strftime('%H:%M:%S')}] "
            "Tempo limite excedido ao enviar a planilha."
        )

        return False

    except requests.exceptions.ConnectionError:

        print(
            f"[{time.strftime('%H:%M:%S')}] "
            "Não foi possível conectar ao PythonAnywhere."
        )

        return False

    except Exception as erro:

        print(
            f"[{time.strftime('%H:%M:%S')}] "
            f"Erro ao enviar: {erro}"
        )

        return False


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():

    # None significa:
    # enviar a planilha uma vez quando iniciar
    ultima = None

    print("=" * 60)
    print("MONITOR DE PLANILHA")
    print("=" * 60)

    print(
        f"Vigiando:\n{CAMINHO_PLANILHA_PC}"
    )

    print(
        f"Servidor:\n{URL}"
    )

    print("=" * 60)

    while True:

        try:

            # Verifica quando a planilha foi modificada
            atual = os.path.getmtime(
                CAMINHO_PLANILHA_PC
            )

            # Se for a primeira execução
            # ou se a planilha foi alterada
            if atual != ultima:

                print(
                    "Planilha alterada. "
                    f"Aguardando {ESTABILIZAR} segundos..."
                )

                # Espera Excel/OneDrive terminarem
                # de salvar o arquivo
                time.sleep(ESTABILIZAR)

                # Verifica novamente
                novo_mtime = os.path.getmtime(
                    CAMINHO_PLANILHA_PC
                )

                # Se ainda estiver mudando,
                # espera o próximo ciclo
                if novo_mtime != atual:

                    print(
                        "A planilha ainda está sendo modificada."
                    )

                    time.sleep(INTERVALO)

                    continue

                print(
                    "Enviando planilha..."
                )

                # Tenta enviar
                if enviar():

                    # Só marca como processada
                    # se o envio realmente funcionou
                    ultima = atual

        except FileNotFoundError:

            print(
                f"[{time.strftime('%H:%M:%S')}] "
                "Planilha não encontrada."
            )

            print(
                "Confira o CAMINHO_PLANILHA_PC."
            )

        except Exception as erro:

            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Erro: {erro}"
            )

        # Aguarda antes de verificar novamente
        time.sleep(INTERVALO)


# ============================================================
# INICIAR PROGRAMA
# ============================================================

if __name__ == "__main__":
    main()