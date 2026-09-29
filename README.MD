# Dashboard de Operação

Dashboard web em Flask com indicadores de Recebimento, Expedição e Etiquetas,
alimentado por uma planilha Excel.

## Como funciona
- O sistema lê `base_operacao.xlsx` (abas RECEBIMENTO, EXPEDICAO e ETIQUETAS)
  e grava os dados em um banco SQLite (`operacao.db`).
- A planilha é monitorada: quando ela é salva, os dados são reimportados
  automaticamente (a cada ~5 segundos).
- Antes de cada importação, é feito um backup na pasta `backups/`.

## Instalação
```
pip install -r requirements.txt
```

## Como rodar
```
python app.py
```
Acesse em http://127.0.0.1:5000. Em outros computadores da rede, use o IP
da máquina, por exemplo http://192.168.x.x:5000.

## Regras dos indicadores
- **% concluído (Etiquetas):** se o pedido tem data de conclusão, conta como
  100% das caixas; senão, usa a coluna CAIXAS ETIQUETADAS.
- **Tempo médio:** diferença entre DATA DE INICIO e DATA DE CONCLUSÃO, em horas.

## Observações
- `base_operacao.xlsx`, `operacao.db` e `backups/` não são versionados
  (contêm dados da operação). Coloque a planilha na pasta do projeto antes
  de rodar.