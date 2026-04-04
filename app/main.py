import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from fastapi import Depends
from contextlib import asynccontextmanager


app = FastAPI()

# Configurações 
SECRET_KEY = "admin"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Para encriptar passwords e ler tokens
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

FHIR_SERVER_URL = os.getenv("FHIR_SERVER_URL", "http://localhost:9000/fhir")

def get_db_connection():
    # Credenciais iguais às do docker-compose
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        database=os.getenv("DB_NAME", "tp1"),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASS", "admin")
    )

def init_db():
    conn = None
    try:
        # Estabelece a ligação ao PostgreSQL (Porta 5432 no Docker)
        conn = get_db_connection()

        # Localiza o ficheiro SQL que contém os comandos CREATE TABLE
        schema_path = os.path.join(os.path.dirname(__file__), "../db/schema.sql")
        
        if os.path.exists(schema_path):
            # Abre e lê o conteúdo do ficheiro schema.sql
            with open(schema_path, "r", encoding="utf-8") as f:
                sql_schema = f.read()
            
            # Executa o script SQL na base de dados e confirma as alterações (commit)
            with conn.cursor() as cur:
                cur.execute(sql_schema)
                conn.commit()
                print("Tabelas inicializadas com sucesso via schema.sql!")
        else:
            print("Ficheiro schema.sql não encontrado. Ignorando init_db.")
            
    except Exception as e:
        # Captura erros de ligação ou de sintaxe no SQL
        print(f"Erro ao inicializar base de dados: {e}")
    finally:
        # Garante que a ligação é fechada, independentemente de ter havido erro ou não
        if conn:
            conn.close()

# Compara a password enviada pelo utilizador com a hash guardada no SQL
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

# Transforma a password num código seguro (Hash) usando a biblioteca Passlib
def get_password_hash(password):
    return pwd_context.hash(password)

# Gera o Token que o utilizador usará nos próximos pedidos
def create_access_token(data: dict):
    to_encode = data.copy()
    # Define o tempo de validade do token (ex: 30 minutos)
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    # Adiciona a data de expiração aos dados do token
    to_encode.update({"exp": expire})
    # Assina o token com a tua SECRET_KEY para garantir que ninguém o consegue falsificar
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Função assíncrona que garante que apenas utilizadores logados com um token válido conseguem aceder aos teus endpoints
async def get_current_user(token: str = Depends(oauth2_scheme)):
    # Define a exceção padrão para erros de autenticação (HTTP 401 Unauthorized)
    credentials_exception = HTTPException(
        status_code=401,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Tenta descodificar o token usando a SECRET_KEY e o Algoritmo
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        # Extrai o "sub" (Subject), que no neste caso guarda o username do utilizador
        username: str = payload.get("sub")
        # Se o username não existir dentro do payload do token, o token é inválido
        if username is None:
            raise credentials_exception
        return username
    # Se o JWT estiver mal formatado, expirado ou a assinatura for falsa, lança o erro 401
    except JWTError:
        raise credentials_exception

# Função que submete um recurso ao endpoint de validação ($validate) do HAPI FHIR    
def validar_recurso_fhir(recurso_json, tipo_recurso):
    # Pergunta ao HAPI se o JSON é um recurso FHIR válido antes de o gravarmos.
    url_valida = f"{FHIR_SERVER_URL}/{tipo_recurso}/$validate"
    try:
        # Envia o JSON para o HAPI apenas para validação (não grava nada ainda)
        res = requests.post(url_valida, json=recurso_json, timeout=5)
        # O HAPI responde sempre com um recurso do tipo "OperationOutcome"
        resultado = res.json()
        
        # Percorre a lista de 'issues' (problemas ou avisos) retornada pelo servidor
        for issue in resultado.get('issue', []):
            if issue.get('severity') == 'error':
                return False, issue.get('diagnostics')
        
        # Se não encontrar erros críticos, o recurso é considerado válido
        return True, "Válido"
    except Exception as e:
        # Caso o servidor HAPI esteja desligado ou a rede falhe
        return False, f"Servidor de validação incontactável: {str(e)}"

# Evento de ciclo de vida que corre automaticamente quando o FastAPI inicia    
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("--- Verificando SQL Local ---")
    # Chama a função que lê o schema.sql e cria as tabelas se necessário
    init_db() 
    
    print("--- Verificando Servidor FHIR ---")
    try:
        # Tenta aceder ao 'CapabilityStatement' (metadata) do servidor HAPI
        res = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=3)
        # Se o HAPI responder com sucesso (200 OK), a integração está ativa
        if res.status_code == 200:
            print("HAPI FHIR: Online e pronto.")
    # Captura falhas de rede ou servidor offline, avisando o administrador no terminal
    except Exception:
        print("HAPI FHIR: Servidor offline.")
    
    # A aplicação "corre" aqui enquanto este yield estiver ativo
    yield
    print("Encerrando aplicação...")

app = FastAPI(lifespan=lifespan)

@app.post("/Register")
async def register(data: dict):
    username = data.get("username")
    password = data.get("password")
    
    # Encriptar a password
    hashed_pw = pwd_context.hash(password) 

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Insere o par Username + Hash na tabela de utilizadores
        cur.execute(
            "INSERT INTO usuarios (username, password_hash) VALUES (%s, %s)",
            (username, hashed_pw)
        )
        # Confirma a criação do novo utilizador
        conn.commit()
        return {"msg": "Utilizador criado! Agora já podes fazer login."}
    except Exception as e:
        # Se o username já existir ou houver erro de ligação, anula a operação
        conn.rollback()
        return {"erro": str(e)}
    finally:
        conn.close()

@app.post("/Login")
async def login(data: dict):
    username = data.get("username")
    password = data.get("password")

    conn = get_db_connection()
    # RealDictCursor permite aceder aos campos pelo nome: user["password_hash"]
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Procura o utilizador na base de dados pelo username enviado
    cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    # Verificação de existência: se o SELECT não devolver nada, o utilizador não existe
    if not user:
        raise HTTPException(status_code=401, detail="Utilizador não encontrado")
    
    # Verifica a password (compara o texto limpo com o hash do SQL)
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Password incorreta")

    # Gerar o Token
    access_token = create_access_token(data={"sub": user["username"]})
    
    # este return é o que o postman vai mostrar 
    return {
        "access_token": access_token, 
        "token_type": "bearer"
    }

@app.post("/Patient")
async def create_patient(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # preparação dos dados e payload fhir 
        nome_paciente = data.get('nome', 'Sem Nome')
        genero_raw = data.get('genero', 'unknown')

        # Preparar Telecoms do Paciente para o FHIR
        fhir_telecoms = [
            {"system": "phone" if t.get('tipo') == "telemóvel" else "email", "value": t.get('valor')}
            for t in data.get('telecom', [])
        ]

        # Preparar Contactos para o FHIR
        fhir_contacts = []
        for con in data.get('contacto', []):
            con_telecoms = [
                {"system": "phone" if tc.get('tipo') == "telemóvel" else "email", "value": tc.get('valor')}
                for tc in con.get('telecom', [])
            ]
            addr = con.get('endereco')
            con_addresses = {"use": "home" if addr.get('tipo') == "casa" else "work", "line": [addr.get('valor')]}
            fhir_contacts.append({
                "relationship": [{"text": "Emergency Contact"}],
                "name": {"family": con.get('nome')},
                "telecom": con_telecoms,
                "address": con_addresses
            })

        fhir_payload = {
            "resourceType": "Patient",
            "active": True,
            "name": [{"text": nome_paciente}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "contact": fhir_contacts
        }

        # validação fhir 
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Patient")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")

        # se estiver válido, insere no sql
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Inserir Paciente principal
        cur.execute(
            "INSERT INTO patients (nome, genero) VALUES (%s, %s) RETURNING id",
            (nome_paciente, genero_raw)
        )
        paciente_id_local = cur.fetchone()['id']

        # Inserir Telecoms do Paciente
        for tel in data.get('telecom', []):
            cur.execute(
                "INSERT INTO telecom (paciente_id, tipo, valor) VALUES (%s, %s, %s)",
                (paciente_id_local, tel.get('tipo'), tel.get('valor'))
            )

        # Inserir Contactos e seus detalhes
        for con in data.get('contacto', []):
            cur.execute(
                "INSERT INTO contacto (paciente_id, nome) VALUES (%s, %s) RETURNING id",
                (paciente_id_local, con.get('nome'))
            )
            contacto_id = cur.fetchone()['id']

            for tel_con in con.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (contacto_id, tel_con.get('tipo'), tel_con.get('valor'))
                )
            
            end_obj = con.get('endereco')

            # 2. Só insere no SQL se o endereço existir no JSON
            if end_obj and isinstance(end_obj, dict):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (
                        contacto_id, 
                        end_obj.get('tipo'), # Agora funciona porque end_obj é o dicionário
                        end_obj.get('valor')
                    )
                )

        conn.commit() # Grava tudo localmente com segurança

        # enviar para o hapi
        hapi_url = f"{FHIR_SERVER_URL}/Patient"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                
                # UPDATE DO fhir_id NO SQL
                cur.execute(
                    "UPDATE patients SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), paciente_id_local)
                )
                conn.commit()
                
                return {
                    "status": "sucesso",
                    "id_local": paciente_id_local,
                    "id_fhir": fhir_id_gerado,
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": paciente_id_local,
                    "msg": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200]
                }
        except Exception as hapi_err:
            return {"status": "aviso", "id_local": paciente_id_local, "msg": "SQL OK, HAPI offline.", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Erro no processamento: {str(e)}")
    finally:
        if conn: 
            cur.close()
            conn.close()

@app.post("/Observation")
async def create_observation(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # preparação e tradução dos id
        refer_string = data.get('refer', '')
        local_patient_id = int(refer_string.split('/')[-1]) if '/' in refer_string else None

        if not local_patient_id:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida. Use 'Patient/ID'")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Buscar o fhir_id do paciente no SQL
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_patient_id,))
        paciente_row = cur.fetchone()

        if not paciente_row or not paciente_row['fhir_id']:
            raise HTTPException(
                status_code=400, 
                detail=f"O paciente local {local_patient_id} não está sincronizado com o HAPI."
            )

        fhir_patient_id = paciente_row['fhir_id']

        # montar o payload e validar no hapi
        obj_codigo = data.get('codigo', {})
        m = data.get('medicao', {})
        
        lista_codigos_fhir = [
            {
                "system": c.get('system'),
                "code": str(c.get('cod')),
                "display": c.get('disp')
            } for c in obj_codigo.get('coding', [])
        ]

        fhir_payload = {
            "resourceType": "Observation",
            "status": data.get('estado'),
            "subject": {"reference": f"Patient/{fhir_patient_id}"},
            "effectiveDateTime": data.get('dataExecucao'),
            "code": {
                "coding": lista_codigos_fhir,
                "text": obj_codigo.get('text')
            },
            "valueQuantity": {
                "value": m.get('valor'),
                "unit": m.get('unidade'),
                "system": m.get('sistema'),
                "code": str(m.get('cod'))
            }
        }

        # validação do fhir (Se falhar, o código para e não grava no SQL)
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Observation")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")
        
        # Inserir Observação
        cur.execute(
            """INSERT INTO observacoes (paciente_id, estado, refer, dataExecucao) 
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (local_patient_id, data.get('estado'), refer_string, data.get('dataExecucao'))
        )
        obs_id = cur.fetchone()['id']

        # Inserir Código
        cur.execute(
            "INSERT INTO codigo (observacoes_id, text) VALUES (%s, %s) RETURNING id",
            (obs_id, obj_codigo.get('text'))
        )
        codigo_id = cur.fetchone()['id']

        # Inserir Codings
        for item in obj_codigo.get('coding', []):
            cur.execute(
                """INSERT INTO coding (codigo_id, system, cod, disp) 
                   VALUES (%s, %s, %s, %s)""",
                (codigo_id, item.get('system'), item.get('cod'), item.get('disp'))
            )

        # Inserir Medição
        cur.execute(
            """INSERT INTO medicao (observacoes_id, valor, unidade, sistema, cod) 
               VALUES (%s, %s, %s, %s, %s)""",
            (obs_id, m.get('valor'), m.get('unidade'), m.get('sistema'), m.get('cod'))
        )
        
        conn.commit() # Confirmar gravação local

        # envio final para o hapi 
        hapi_url = f"{FHIR_SERVER_URL}/Observation"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_obs_id = hapi_res.json().get('id')
                
                # Atualizar o fhir_id gerado
                cur.execute(
                    "UPDATE observacoes SET fhir_id = %s WHERE id = %s",
                    (str(fhir_obs_id), obs_id)
                )
                conn.commit()
                
                return {
                    "status": "sucesso",
                    "id_local": obs_id,
                    "id_fhir": fhir_obs_id,
                    "validação": "Passou no Schema FHIR"
                }
            else:
                return {"erro": "HAPI recusou", "detalhe": hapi_res.text}

        except Exception as hapi_err:
            return {"status": "Aviso", "msg": "Gravado no SQL, mas HAPI offline", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.post("/Practitioner")
async def create_practitioner(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # preparação dos dados 
        nome_medico = data.get('nome', 'Médico Desconhecido')
        genero_raw = data.get('genero', 'unknown')
        especialidade = data.get('especialidade', 'Clínica Geral')

        # Mapeamento de Contactos/Telecoms para o FHIR (extraído do primeiro contacto do JSON)
        fhir_telecoms = []
        fhir_addresses = []
        contactos_input = data.get('contacto', [])

        if contactos_input:
            primeiro_con = contactos_input[0]
            # Prepara Telecoms (Lista)
            fhir_telecoms = [
                {"system": "phone" if tc.get('tipo') == "telemóvel" else "email", "value": tc.get('valor')}
                for tc in primeiro_con.get('telecom', [])
            ]
            # Prepara Endereço (Objeto Único para Practitioner no FHIR)
            addr_in = primeiro_con.get('endereco')
            if addr_in and isinstance(addr_in, dict):
                fhir_addresses = [{
                    "use": "work" if addr_in.get('tipo') == "trabalho" else "home",
                    "line": [addr_in.get('valor')]
                }]

        fhir_payload = {
            "resourceType": "Practitioner",
            "active": True,
            "name": [{"text": nome_medico}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "address": fhir_addresses,
            "qualification": [{"code": {"text": especialidade}}]
        }

        # validação do fhir (Se falhar, o código para e não grava no SQL)
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Practitioner")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Practitioner: {mensagem}")

        # inserir no sql
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute(
            "INSERT INTO medicos (nome, genero, especialidade) VALUES (%s, %s, %s) RETURNING id",
            (nome_medico, genero_raw, especialidade)
        )
        medico_id_local = cur.fetchone()['id']

        # Tabela 'contacto', 'telecom' e 'endereco'
        for con in contactos_input:
            cur.execute(
                "INSERT INTO contacto (medico_id, nome) VALUES (%s, %s) RETURNING id",
                (medico_id_local, con.get('nome'))
            )
            c_id = cur.fetchone()['id']
            
            # Telecoms do contacto
            for t in con.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (contacto_id, medico_id, tipo, valor) VALUES (%s, %s, %s, %s)",
                    (c_id, medico_id_local, t.get('tipo'), t.get('valor'))
                )
            
            # Endereço do contacto
            e_obj = con.get('endereco')
            if e_obj and isinstance(e_obj, dict):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (c_id, e_obj.get('tipo'), e_obj.get('valor'))
                )

        conn.commit()

        # envio para o hapi 
        hapi_url = f"{FHIR_SERVER_URL}/Practitioner"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)

            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')

                # Atualizar o fhir_id no SQL para manter a sincronização
                cur.execute(
                    "UPDATE medicos SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), medico_id_local)
                )
                conn.commit()

                return {
                    "status": "sucesso",
                    "id_local": medico_id_local,
                    "id_fhir": fhir_id_gerado,
                    "mensagem": "Médico criado e sincronizado!"
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": medico_id_local,
                    "mensagem": "Gravado localmente, mas falhou no HAPI final.",
                    "erro_hapi": hapi_res.text[:200]
                }
        except Exception as hapi_err:
            return {
                "status": "aviso",
                "id_local": medico_id_local,
                "mensagem": "Gravado localmente, mas HAPI offline.",
                "detalhe": str(hapi_err)
            }

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    finally:
        if conn:
            cur.close()
            conn.close()


@app.post("/Encounter")
async def create_encounter(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # Extração dos IDs locais
        ref_paciente_local = data.get('refer_paciente', '')
        id_paciente_sql = int(ref_paciente_local.split('/')[-1]) if '/' in ref_paciente_local else None

        ref_medico_local = data.get('refer_medico', '')
        id_medico_sql = int(ref_medico_local.split('/')[-1]) if '/' in ref_medico_local else None

        if not id_paciente_sql:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida.")

        # tradução de ids 
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Buscar fhir_id do Paciente
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (id_paciente_sql,))
        row_p = cur.fetchone()
        if not row_p or not row_p['fhir_id']:
            raise HTTPException(status_code=400, detail="Paciente local não sincronizado com HAPI.")
        fhir_id_paciente = row_p['fhir_id']

        # Buscar fhir_id do Médico 
        fhir_id_medico = None
        if id_medico_sql:
            cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (id_medico_sql,))
            row_m = cur.fetchone()
            if row_m and row_m['fhir_id']:
                fhir_id_medico = row_m['fhir_id']

        # preparar e validar payload 
        lista_participantes = []
        if fhir_id_medico:
            lista_participantes.append({
                "individual": {"reference": f"Practitioner/{fhir_id_medico}"}
            })

        fhir_payload = {
            "resourceType": "Encounter",
            "status": "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB", 
                "display": "ambulatory"
            },
            "subject": {"reference": f"Patient/{fhir_id_paciente}"},
            "participant": lista_participantes,
            "period": {"start": data.get('data_consulta')},
            "type": [{"text": data.get('tipo_consulta')}]
        }

        # validação fhir 
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Encounter")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Encounter: {mensagem}")

        # inserir no sql 
        try:
            # Inserir consulta
            cur.execute(
                """INSERT INTO consultas (paciente_id, medico_id, data_consulta, tipo_consulta) 
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (id_paciente_sql, id_medico_sql, data.get('data_consulta'), data.get('tipo_consulta'))
            )
            consulta_id_local = cur.fetchone()['id']

            # Inserir no histórico
            cur.execute(
                "INSERT INTO historico (paciente_id, consulta_id) VALUES (%s, %s)",
                (id_paciente_sql, consulta_id_local)
            )
            conn.commit()
        except Exception as sql_err:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Erro ao gravar no SQL local: {str(sql_err)}")

        # envio para o hapi 
        hapi_url = f"{FHIR_SERVER_URL}/Encounter"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')

                # Atualizar o fhir_id na tabela consultas
                cur.execute(
                    "UPDATE consultas SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), consulta_id_local)
                )
                conn.commit()

                return {
                    "status": "sucesso",
                    "id_local": consulta_id_local,
                    "id_fhir": fhir_id_gerado,
                    "info": "Consulta e Histórico criados e sincronizados."
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": consulta_id_local,
                    "mensagem": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200]
                }

        except Exception as hapi_err:
            return {"status": "aviso", "id_local": consulta_id_local, "msg": "SQL OK, HAPI offline.", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.get("/Patient/{local_id}")
async def get_patient(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Traduzir o ID local do paciente para o fhir_id do HAPI
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Paciente não existe no SQL")
        
        fhir_id = result.get('fhir_id')
        
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Paciente existe no SQL, mas não foi sincronizado com o HAPI")

        # Ir buscar ao HAPI usando o fhir_id
        hapi_url = f"{FHIR_SERVER_URL}/Patient/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "recurso_fhir_completo": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar no HAPI")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Observation/{local_id}")
async def get_observation(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Traduzir o ID local do paciente para o fhir_id do HAPI
        cur.execute("SELECT fhir_id FROM observacoes WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Observação não encontrada no SQL")
        
        fhir_id = result.get('fhir_id')
        
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Observação sem mapeamento FHIR (fhir_id é null)")

        # Ir buscar ao HAPI usando o fhir_id
        hapi_url = f"{FHIR_SERVER_URL}/Observation/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "dados_provenientes_do_hapi": response.json()
            }
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail="Observação não encontrada no servidor HAPI")
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro na comunicação com HAPI")

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@app.get("/Observation")
async def get_patient_observations(patient: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Traduzir o ID local do paciente para o fhir_id do HAPI
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (patient,))
        res_paciente = cur.fetchone()

        if not res_paciente or not res_paciente['fhir_id']:
            raise HTTPException(
                status_code=404, 
                detail="Paciente não encontrado ou não sincronizado com o HAPI"
            )

        fhir_patient_id = res_paciente['fhir_id']

        # Consultar o HAPI usando o filtro de paciente (?patient=ID)
        hapi_url = f"{FHIR_SERVER_URL}/Observation?patient={fhir_patient_id}"
        
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            # O HAPI devolve lista de entradas
            fhir_data = response.json()
            
            # Extraímos apenas a lista de observações para ser mais fácil de ler
            observacoes = []
            if "entry" in fhir_data:
                for entry in fhir_data["entry"]:
                    observacoes.append(entry["resource"])

            return {
                "id_local_paciente": patient,
                "id_fhir_paciente": fhir_patient_id,
                "total_observacoes": len(observacoes),
                "lista_observacoes": observacoes
            }
        else:
            raise HTTPException(
                status_code=response.status_code, 
                detail="Erro ao procurar observações no HAPI"
            )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.get("/Practitioner/{local_id}")
async def get_practitioner(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Procurar o fhir_id na tua tabela local 'medicos'
        cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Médico não existe no SQL local")

        fhir_id = result.get('fhir_id')

        if not fhir_id:
            raise HTTPException(status_code=404, detail="Médico local sem fhir_id (não sincronizado com o HAPI)")

        # 2. Ir buscar ao HAPI os dados completos
        hapi_url = f"{FHIR_SERVER_URL}/Practitioner/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        
        try:
            response = requests.get(hapi_url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                return {
                    "id_local": local_id,
                    "id_fhir": fhir_id,
                    "recurso_fhir_do_servidor": response.json()
                }
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Médico não encontrado no servidor HAPI")
            else:
                raise HTTPException(status_code=response.status_code, detail="Erro ao comunicar com HAPI")
        
        except Exception as hapi_err:
            raise HTTPException(status_code=503, detail=f"Servidor HAPI incontactável: {str(hapi_err)}")

    except Exception as e:
        if isinstance(e, HTTPException): 
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.get("/Encounter/{consulta_id}")
async def get_encounter(consulta_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Procurar o fhir_id na tua tabela local 'consultas'
        cur.execute("SELECT fhir_id FROM consultas WHERE id = %s", (consulta_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Consulta não encontrada no SQL local")
        
        fhir_id = result.get('fhir_id')
        
        if not fhir_id:
            raise HTTPException(
                status_code=404, 
                detail="Esta consulta existe no SQL, mas não tem um fhir_id (não foi sincronizada com o HAPI)"
            )

        # Ir buscar o recurso ao HAPI usando o fhir_id
        hapi_url = f"{FHIR_SERVER_URL}/Encounter/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        
        try:
            response = requests.get(hapi_url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                return {
                    "id_local": consulta_id,
                    "id_fhir_no_hapi": fhir_id,
                    "recurso_fhir_completo": response.json()
                }
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Consulta não encontrada no servidor HAPI")
            else:
                raise HTTPException(status_code=response.status_code, detail="Erro na resposta do servidor HAPI")

        except Exception as hapi_err:
            raise HTTPException(status_code=503, detail=f"Servidor HAPI incontactável: {str(hapi_err)}")

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Patient/{local_id}/History")
async def get_patient_history(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Descobrir qual é o ID do HAPI para este paciente
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()
        
        if not result or not result['fhir_id']:
            raise HTTPException(status_code=404, detail="Paciente não encontrado ou sem ID FHIR")

        fhir_id = result['fhir_id']

        # Pedir ao HAPI todas as Observations deste subject
        hapi_url = f"{FHIR_SERVER_URL}/Observation?subject=Patient/{fhir_id}"
        response = requests.get(hapi_url)

        if response.status_code == 200:
            return response.json() # Retorna o Bundle do FHIR com o histórico
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")

    finally:
        if conn: conn.close()