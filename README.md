# Gestao e Agenda

Aplicacao Windows em Python/Tkinter para gerir clientes, prestadores, servicos,
aulas, terapias e marcacoes numa agenda semanal por prestador.

A janela principal funciona como um formulario central, ao estilo MDI: cada
modulo abre numa janela separada para permitir trabalhar com clientes,
prestadores, servicos, agenda e backup ao mesmo tempo.

## Executar

```powershell
python app.py
```

Na primeira execucao a aplicacao cria automaticamente o ficheiro `agenda.db` na
mesma pasta do `app.py`.

Quando a aplicacao encontra uma base antiga, atualiza automaticamente a
estrutura para a versao mais recente. Antes dessa atualizacao cria uma copia de
seguranca com o nome `agenda_backup_pre_migration_YYYYMMDD_HHMMSS.db`.

## Funcionalidades

- Login de utilizadores com password.
- Gestao de utilizadores e acessos aos modulos da aplicacao.
- Cadastro de clientes.
- Cadastro de prestadores, com servico associado opcional.
- Cadastro unico de servicos com tipo `Aula`, `Terapia` ou `Servico`,
  prestador associado e valor.
- No cadastro de clientes e possivel selecionar uma aula, ver automaticamente
  o prestador e o valor em leitura, e definir data de inicio e data de fim.
- No cadastro de clientes existe uma listagem de valores com filtro por nome,
  mostrando valor da aula, prestador, 70% para o prestador, 30% para Luz
  Dourada e estado de pagamento.
- Janela de pagamentos para registar valores pendentes, pagos, parciais ou
  cancelados por cliente, aula/servico e periodo.
- Agenda semanal em grelha horaria, com filtro por ano e prestador.
- Horario da grelha configuravel por hora inicial, hora final e blocos de 15,
  30 ou 60 minutos.
- Marcacoes com prestador, servico, data, hora inicial, hora final, estado e
  notas.
- Cores por estado da marcacao: `Marcado`, `Concluido` e `Cancelado`.
- Clique num bloco livre para preparar uma nova marcacao e duplo clique numa
  marcacao para editar.
- Arraste uma marcacao dentro da grelha para alterar o dia ou a hora mantendo a
  mesma duracao.
- O prestador aparece em destaque no topo de cada bloco da agenda.
- As notas da marcacao aparecem ao passar o cursor por cima do bloco na agenda.
- Aviso de conflito quando existe sobreposicao de horario para o mesmo
  prestador.
- Backup completo da base SQLite para um ficheiro `.db`.

## Login inicial

Quando ainda nao existir nenhum utilizador, a aplicacao cria automaticamente:

- Utilizador: `admin`
- Password: `admin`

Depois de entrar pela primeira vez, altere esta password no modulo
`Utilizadores`.

## Gerar executavel

Com PyInstaller instalado:

```powershell
pyinstaller AgendaClientes.spec
```

O executavel sera criado em `dist\AgendaClientes.exe`.

## Backup

Use a aba `Backup` para guardar uma copia da base de dados. Para restaurar
manualmente, feche a aplicacao e substitua o ficheiro `agenda.db` pela copia
guardada.

## Neon

Este projeto local esta ligado ao projeto Neon `Agenda`:

- Organizacao: `org-morning-morning-02280560`
- Projeto: `crimson-hill-22508659`
- Branch: `production`

A Neon CLI guardou as variaveis em `.env.local`. Este ficheiro contem segredos
e esta ignorado no `.gitignore`. Use `.env.example` apenas como referencia dos
nomes das variaveis.

O schema PostgreSQL da aplicacao esta em `migrations/neon_schema.sql` e ja foi
aplicado na branch `production`.

No PowerShell deste Windows, use `neon.cmd` em vez de `neon` se a politica de
execucao bloquear ficheiros `.ps1`.

## Sincronizacao local para Neon

A aplicacao Windows continua a trabalhar com a base local `agenda.db`. Para a
consulta no telemovel, a primeira abordagem e sincronizar essa base local para o
Neon atraves de uma API.

Instalar dependencias da API:

```powershell
python -m pip install -r requirements-api.txt
```

Arrancar a API local:

```powershell
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Sincronizar o `agenda.db` local para o Neon:

```powershell
python sync_to_neon.py
```

Tambem pode usar o botao `Sincronizar Neon` na janela principal da aplicacao
Windows. A API deve estar ligada antes de carregar no botao.

Nesta primeira versao, a sincronizacao envia e atualiza dados no Neon sem apagar
registos remotos. A app mobile devera consultar a API; a escrita continua a ser
feita na aplicacao Windows.

## Deploy da API no Render

O projeto inclui `render.yaml` e `requirements.txt` para publicar a API FastAPI
no Render.

No Render:

1. Criar um novo `Web Service`.
2. Ligar ao repositorio GitHub deste projeto.
3. Confirmar:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
4. Criar as variaveis de ambiente:
   - `DATABASE_URL`: usar o valor do Neon.
   - `SYNC_API_KEY`: criar uma chave/password propria para sincronizacao.

Depois do deploy, testar:

```text
https://NOME-DO-SERVICO.onrender.com/health
```

Pagina mobile simples para consultar marcacoes:

```text
https://NOME-DO-SERVICO.onrender.com/mobile
```

Na maquina Windows, atualizar `AGENDA_API_URL` no `.env.local` para o endereco
do Render, por exemplo:

```text
AGENDA_API_URL=https://NOME-DO-SERVICO.onrender.com
```
