import psycopg2
import requests
from fastapi import FastAPI, HTTPException, Depends
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
import time

app = FastAPI()

# Configurações 
SECRET_KEY = "admin"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Configurações EHRbase
EHRBASE_URL = "http://ehrbase:8080/ehrbase/rest/openehr/v1"
EHRBASE_URL_ADMIN = "http://ehrbase:8080/ehrbase/rest/openehr/v1" # Alterado para a porta interna correta do endpoint REST
EHRBASE_USER = "admin-user"
EHRBASE_PASS = "RequirementPassword"
EHR_AUTH = (EHRBASE_USER, EHRBASE_PASS)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

FHIR_SERVER_URL = "http://fhir:8080/fhir"

def get_db_connection():
    return psycopg2.connect(
        host="db",  
        port=5432,
        database="tp1",
        user="admin",
        password="admin"
    )

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        return username
    except JWTError:
        raise credentials_exception

def validar_recurso_fhir(recurso_json, tipo_recurso):
    url_valida = f"{FHIR_SERVER_URL}/{tipo_recurso}/$validate"
    try:
        res = requests.post(url_valida, json=recurso_json, timeout=5)
        resultado = res.json()
        
        for issue in resultado.get('issue', []):
            if issue.get('severity') == 'error':
                return False, issue.get('diagnostics')
        return True, "Válido"
    except Exception as e:
        return False, f"Servidor de validação incontactável: {str(e)}"

def get_or_create_ehr(numero_utente, patient_fhir_id):
    search_url_1 = f"{EHRBASE_URL}/ehr?subject_id={patient_fhir_id}&subject_namespace=pt-sns-utente"
    res = requests.get(search_url_1, auth=EHR_AUTH)
    if res.status_code == 200:
        return res.json()['ehr_id']['value']
        
    search_url_2 = f"{EHRBASE_URL}/ehr?subject_id={patient_fhir_id}&subject_namespace=pt.sns.utente"
    res_legacy = requests.get(search_url_2, auth=EHR_AUTH)
    if res_legacy.status_code == 200:
        return res_legacy.json()['ehr_id']['value']
    
    payload = {
        "_type": "EHR_STATUS",
        "archetype_node_id": "openEHR-EHR-EHR_STATUS.generic.v1",
        "name": {
            "_type": "DV_TEXT",
            "value": "EHR Status"
        },
        "is_queryable": True,
        "is_modifiable": True,
        "subject": {
            "_type": "PARTY_SELF",
            "external_ref": {
                "_type": "PARTY_REF",
                "id": {
                    "_type": "GENERIC_ID", 
                    "value": str(patient_fhir_id), 
                    "scheme": "fhir"
                },
                "namespace": "pt-sns-utente",
                "type": "PERSON"
            }
        }
    }
    
    create_res = requests.post(f"{EHRBASE_URL}/ehr", json=payload, auth=EHR_AUTH)
    
    if create_res.status_code in [200, 201]:
        if 'Location' in create_res.headers:
            return create_res.headers['Location'].split('/')[-1]
        elif 'ETag' in create_res.headers:
            return create_res.headers['ETag'].replace('"', '')
        return create_res.json()['ehr_id']['value']
        
    if create_res.status_code == 409:
        retry = requests.get(search_url_1, auth=EHR_AUTH)
        if retry.status_code == 200:
            return retry.json()['ehr_id']['value']
        retry_legacy = requests.get(search_url_2, auth=EHR_AUTH)
        if retry_legacy.status_code == 200:
            return retry_legacy.json()['ehr_id']['value']

    print(f"❌ Erro crítico do EHRbase: {create_res.text}")
    raise Exception(f"EHRbase devolveu status {create_res.status_code}: {create_res.text}")

def upload_template():
    """Tenta fazer o upload do template .opt para o EHRbase usando caminhos absolutos"""
    url = f"{EHRBASE_URL}/definition/template/adl1.4"
    
    # --- CORREÇÃO DE CAMINHO ABSOLUTO ---
    # Descobre dinamicamente onde está o main.py (ex: /app) e junta com /templates
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, "templates")
    template_path = os.path.join(templates_dir, "sinais_vitais.opt")
    
    print(f"📁 A API está a procurar o template em: {template_path}")
    
    if not os.path.exists(template_path):
        print(f"❌ Erro: O ficheiro {template_path} não existe fisicamente.")
        # Diagnóstico: Vamos listar tudo o que está dentro da pasta para ver o nome real do ficheiro
        try:
            arquivos_encontrados = os.listdir(templates_dir)
            print(f"🔍 Ficheiros que realmente existem dentro de {templates_dir}: {arquivos_encontrados}")
        except Exception as e:
            print(f"⚠️ Não foi possível listar a pasta de templates: {e}")
        return

    # Se o ficheiro existe, avança para o upload normal
    for i in range(30): 
        try:
            with open(template_path, "rb") as f:
                headers = {'Content-Type': 'application/xml'}
                res = requests.post(url, data=f, auth=EHR_AUTH, headers=headers)
                if res.status_code in [200, 201]:
                    print("✅ Passo 1: Template openEHR carregado com sucesso!")
                    return
                else:
                    print(f"⚠️ Aguardando EHRbase... Status: {res.status_code} (Tentativa {i+1}/30)")
        except Exception as e:
            print(f"⚠️ EHRbase ainda não responde... ({str(e)}) (Tentativa {i+1}/30)")
        time.sleep(5)

MAPA_SINAIS_VITAIS = {
    "8480-6": {
        "nome": "Blood pressure", 
        "archetype": "openEHR-EHR-OBSERVATION.blood_pressure.v2", 
        "node": "at0004" # Systolic
    },
    "8462-4": {
        "nome": "Blood pressure", 
        "archetype": "openEHR-EHR-OBSERVATION.blood_pressure.v2", 
        "node": "at0005" # Diastolic
    },
    "8867-4": {
        "nome": "Pulse/Heart beat", 
        "archetype": "openEHR-EHR-OBSERVATION.pulse.v2", 
        "node": "at0004" # Rate
    },
    "8310-5": {
        "nome": "Body temperature", 
        "archetype": "openEHR-EHR-OBSERVATION.body_temperature.v2", 
        "node": "at0004" # Temperature
    },
    "59408-5": {
        "nome": "Pulse oximetry", 
        "archetype": "openEHR-EHR-OBSERVATION.pulse_oximetry.v1", 
        "node": "at0006" # Corrigido para bater com o teu template
    },
    "29463-7": {
        "nome": "Body weight", 
        "archetype": "openEHR-EHR-OBSERVATION.body_weight.v2", 
        "node": "at0004" # Weight
    },
    "9279-1": {
        "nome": "Respiration", 
        "archetype": "openEHR-EHR-OBSERVATION.respiration.v2", 
        "node": "at0004" # Rate
    }
}

def build_openehr_composition(fhir_payload: dict, current_user: str) -> dict:
    try:
        coding_list = fhir_payload.get("code", {}).get("coding", [])
        if not coding_list:
            return None
        
        codigo_loinc = coding_list[0].get("code")
        
        if codigo_loinc not in MAPA_SINAIS_VITAIS:
            print(f"⚠️ Código LOINC {codigo_loinc} não mapeado.")
            return None
            
        config = MAPA_SINAIS_VITAIS[codigo_loinc]
        archetype = config["archetype"]
        
        valor = fhir_payload.get("valueQuantity", {}).get("value")
        unidade = fhir_payload.get("valueQuantity", {}).get("unit")
        data_execucao = fhir_payload.get("effectiveDateTime")
        
        if data_execucao and data_execucao.endswith('Z'):
            data_execucao = data_execucao.replace('Z', '')

        # FORMATO FLAT COM AS MAIÚSCULAS EXATAS EXIGIDAS PELO TEU TEMPLATE
        composition = {
            "ctx/language": "en",  
            "ctx/territory": "DE", 
            "ctx/composer_name": current_user,
            "ctx/time": data_execucao,
        }

        # MAUÉSCULAS E ESPAÇOS ALINHADOS A 100% COM O TEU ESQUELETO /EXAMPLE
        if codigo_loinc == "59408-5":
            # Saturação de Oxigénio (pulse_oximetry.v1)
            # Corrigido para "Any event:0" e "SpO₂" com o ₂ correto do unicode
            composition[f"{archetype}/Any event:0/SpO₂|numerator"] = valor
            composition[f"{archetype}/Any event:0/SpO₂|type"] = 3
            composition[f"{archetype}/Any event:0/time"] = data_execucao
            composition[f"{archetype}/origin"] = data_execucao
            
        elif codigo_loinc in ["8480-6", "8462-4"]:
            # Pressão Arterial (blood_pressure.v2)
            # No teu exemplo os sub-nós chamam-se "Systolic" e "Diastolic"
            composition[f"{archetype}/Any event:0/time"] = data_execucao
            composition[f"{archetype}/origin"] = data_execucao
            if codigo_loinc == "8480-6":
                composition[f"{archetype}/Any event:0/Systolic|magnitude"] = valor
                composition[f"{archetype}/Any event:0/Systolic|units"] = unidade
            else:
                composition[f"{archetype}/Any event:0/Diastolic|magnitude"] = valor
                composition[f"{archetype}/Any event:0/Diastolic|units"] = unidade

        elif codigo_loinc == "8867-4":
            # Frequência Cardíaca (pulse.v2)
            # No teu exemplo chama-se "Rate" dentro de "Any event:0"
            composition[f"{archetype}/Any event:0/Rate|magnitude"] = valor
            composition[f"{archetype}/Any event:0/Rate|units"] = unidade
            composition[f"{archetype}/Any event:0/time"] = data_execucao
            composition[f"{archetype}/origin"] = data_execucao

        elif codigo_loinc == "8310-5":
            # Temperatura Corporal (body_temperature.v2)
            # No teu exemplo chama-se "Temperature"
            composition[f"{archetype}/Any event:0/Temperature|magnitude"] = valor
            composition[f"{archetype}/Any event:0/Temperature|units"] = unidade
            composition[f"{archetype}/Any event:0/time"] = data_execucao
            composition[f"{archetype}/origin"] = data_execucao

        elif codigo_loinc == "29463-7":
            # Peso Corporal (body_weight.v2)
            # No teu exemplo chama-se "Weight"
            composition[f"{archetype}/Any event:0/Weight|magnitude"] = valor
            composition[f"{archetype}/Any event:0/Weight|units"] = unidade
            composition[f"{archetype}/Any event:0/time"] = data_execucao
            composition[f"{archetype}/origin"] = data_execucao

        elif codigo_loinc == "9279-1":
            # Frequência Respiratória (respiration.v2)
            # No teu exemplo chama-se "Rate"
            composition[f"{archetype}/Any event:0/Rate|magnitude"] = valor
            composition[f"{archetype}/Any event:0/Rate|units"] = unidade
            composition[f"{archetype}/Any event:0/time"] = data_execucao
            composition[f"{archetype}/origin"] = data_execucao
        
        return composition
        
    except Exception as e:
        print(f"❌ Erro crítico ao construir composição openEHR: {e}")
        return None

@app.on_event("startup")
async def startup_event():
    print("--- Inicializando Middleware e Templates ---")
    upload_template()
    
    print("--- Verificando Servidor FHIR ---")
    try:
        res = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=3)
        if res.status_code == 200:
            print("HAPI FHIR: Online e pronto.")
    except Exception:
        print("HAPI FHIR: Servidor offline ou a iniciar.")

@app.post("/Register")
async def register(data: dict):
    username = data.get("username")
    password = data.get("password")
    hashed_pw = pwd_context.hash(password) 

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO usuarios (username, password_hash) VALUES (%s, %s)",
            (username, hashed_pw)
        )
        conn.commit()
        return {"msg": "Utilizador criado! Agora já podes fazer login."}
    except Exception as e:
        conn.rollback()
        return {"erro": str(e)}
    finally:
        conn.close()

@app.post("/Login")
async def login(data: dict):
    username = data.get("username")
    password = data.get("password")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Utilizador não encontrado")
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Password incorreta")

    access_token = create_access_token(data={"sub": user["username"]})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/Patient")
async def create_patient(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        nome_paciente = data.get('nome', 'Sem Nome')
        genero_raw = data.get('genero', 'unknown')

        fhir_telecoms = [
            {"system": "phone" if t.get('tipo') == "telemóvel" else "email", "value": t.get('valor')}
            for t in data.get('telecom', [])
        ]

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

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Patient")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            "INSERT INTO patients (nome, genero) VALUES (%s, %s) RETURNING id",
            (nome_paciente, genero_raw)
        )
        paciente_id_local = cur.fetchone()['id']

        for tel in data.get('telecom', []):
            cur.execute(
                "INSERT INTO telecom (paciente_id, tipo, valor) VALUES (%s, %s, %s)",
                (paciente_id_local, tel.get('tipo'), tel.get('valor'))
            )

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
            if end_obj and isinstance(end_obj, dict):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (contacto_id, end_obj.get('tipo'), end_obj.get('valor'))
                )

        conn.commit()

        hapi_url = f"{FHIR_SERVER_URL}/Patient"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}
        ehrbase_status = "Pendente (HAPI Falhou)"
        ehr_id_gerado = None

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                cur.execute("UPDATE patients SET fhir_id = %s WHERE id = %s", (str(fhir_id_gerado), paciente_id_local))
                conn.commit()
                
                try:
                    ehr_id_gerado = get_or_create_ehr(str(paciente_id_local), str(fhir_id_gerado))
                    ehrbase_status = "Sincronizado"
                except Exception as ehr_err:
                    ehrbase_status = f"Erro ao criar no openEHR: {str(ehr_err)}"
                
                return {
                    "status": "sucesso",
                    "id_local": paciente_id_local,
                    "id_fhir": fhir_id_gerado,
                    "sincronizacao_openehr": {
                        "status": ehrbase_status,
                        "ehr_id": ehr_id_gerado
                    }
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": paciente_id_local,
                    "msg": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200],
                    "sincronizacao_openehr": {"status": ehrbase_status}
                }
        except Exception as hapi_err:
            return {
                "status": "aviso", 
                "id_local": paciente_id_local, 
                "msg": "SQL OK, HAPI offline.", 
                "erro": str(hapi_err),
                "sincronizacao_openehr": {"status": "Pendente (HAPI Offline)"}
            }

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
        refer_string = data.get('refer', '')
        local_patient_id = int(refer_string.split('/')[-1]) if '/' in refer_string else None

        if not local_patient_id:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida. Use 'Patient/ID'")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT fhir_id, nome FROM patients WHERE id = %s", (local_patient_id,))
        paciente_row = cur.fetchone()

        if not paciente_row or not paciente_row['fhir_id']:
            raise HTTPException(
                status_code=400, 
                detail=f"O paciente local {local_patient_id} não está sincronizado com o HAPI."
            )

        fhir_patient_id = paciente_row['fhir_id']

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
            "subject": {
                "reference": f"Patient/{fhir_patient_id}",
                "identifier": {
                    "system": "http://minhaapi.local/identifiers/patient",
                    "value": str(local_patient_id)
                }
            },
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

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Observation")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")
        
        cur.execute(
            """INSERT INTO observacoes (paciente_id, estado, refer, dataExecucao) 
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (local_patient_id, data.get('estado'), refer_string, data.get('dataExecucao'))
        )
        obs_id = cur.fetchone()['id']

        cur.execute(
            "INSERT INTO codigo (observacoes_id, text) VALUES (%s, %s) RETURNING id",
            (obs_id, obj_codigo.get('text'))
        )
        codigo_id = cur.fetchone()['id']

        for item in obj_codigo.get('coding', []):
            cur.execute(
                """INSERT INTO coding (codigo_id, system, cod, disp) 
                   VALUES (%s, %s, %s, %s)""",
                (codigo_id, item.get('system'), item.get('cod'), item.get('disp'))
            )

        cur.execute(
            """INSERT INTO medicao (observacoes_id, valor, unidade, sistema, cod) 
               VALUES (%s, %s, %s, %s, %s)""",
            (obs_id, m.get('valor'), m.get('unidade'), m.get('sistema'), m.get('cod'))
        )
        conn.commit()

        hapi_url = f"{FHIR_SERVER_URL}/Observation"
        headers_fhir = {"Content-Type": "application/fhir+json;charset=utf-8"}
        fhir_obs_id = None

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers_fhir, timeout=5)
            if hapi_res.status_code in [200, 201]:
                fhir_obs_id = hapi_res.json().get('id')
                cur.execute("UPDATE observacoes SET fhir_id = %s WHERE id = %s", (str(fhir_obs_id), obs_id))
                conn.commit()
            else:
                print(f"⚠️ HAPI rejeitou a submissão: {hapi_res.text}")
        except Exception as hapi_err:
            print(f"⚠️ Servidor HAPI FHIR offline: {hapi_err}")

        # ---------------------------------------------------------------------
        # 5. Integração Automática com o openEHR (EHRbase)
        # ---------------------------------------------------------------------
        utente_sns = str(local_patient_id) 
        ehrbase_sync_status = "Não Sincronizado"
        comp_uid = None

        try:
            ehr_id = get_or_create_ehr(utente_sns, fhir_patient_id)
            composition = build_openehr_composition(fhir_payload, current_user)
            
            if composition:
                comp_url = f"{EHRBASE_URL}/ehr/{ehr_id}/composition?templateId=sinais_vitais&format=FLAT"
                comp_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Prefer": "return=representation"
                }
                
                res_ehr = requests.post(comp_url, json=composition, auth=EHR_AUTH, headers=comp_headers, timeout=5)
                
                if res_ehr.status_code in [200, 201]:
                    ehrbase_sync_status = "Sucesso"
                    comp_uid = res_ehr.json().get('uid', {}).get('value')
                else:
                    ehrbase_sync_status = f"Erro EHRbase ({res_ehr.status_code})"
                    print(f"❌ TEXTO DE REJEIÇÃO DO EHRBASE ({res_ehr.status_code}): {res_ehr.text}")
                    raise HTTPException(
                        status_code=res_ehr.status_code, 
                        detail=f"EHRbase rejeitou a composição: {res_ehr.text}"
                    )
            else:
                # Se a função build_openehr_composition devolveu None, avisa imediatamente no Postman
                raise HTTPException(
                    status_code=400, 
                    detail="A tradução para openEHR falhou. Verifica se o LOINC enviado está mapeado no MAPA_SINAIS_VITAIS."
                )
                
        except Exception as ehr_err:
            # Se for uma HTTPException lançada por nós, propaga para o Postman
            if isinstance(ehr_err, HTTPException): 
                raise ehr_err
            # Se for um erro bruto de Python (ex: KeyError, NameError, etc.), mostra o erro real no Postman
            raise HTTPException(
                status_code=500, 
                detail=f"Erro interno no bloco openEHR: {str(ehr_err)}"
            )

        # ---------------------------------------------------------------------
        # 6. Resposta Unificada (Apenas chega aqui se der Sucesso Real)
        # ---------------------------------------------------------------------
        return {
            "status": "sucesso",
            "id_local_sql": obs_id,
            "id_fhir_hapi": fhir_obs_id if fhir_obs_id else "Falhou/Offline",
            "sincronizacao_openehr": {
                "status": ehrbase_sync_status,
                "ehr_id": ehr_id,
                "composition_uid": comp_uid
            }
        }

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
        nome_medico = data.get('nome', 'Médico Desconhecido')
        genero_raw = data.get('genero', 'unknown')
        especialidade = data.get('especialidade', 'Clínica Geral')

        fhir_telecoms = []
        fhir_addresses = []
        contactos_input = data.get('contacto', [])

        if contactos_input:
            primeiro_con = contactos_input[0]
            fhir_telecoms = [
                {"system": "phone" if tc.get('tipo') == "telemóvel" else "email", "value": tc.get('valor')}
                for tc in primeiro_con.get('telecom', [])
            ]
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

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Practitioner")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Practitioner: {mensagem}")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute(
            "INSERT INTO medicos (nome, genero, especialidade) VALUES (%s, %s, %s) RETURNING id",
            (nome_medico, genero_raw, especialidade)
        )
        medico_id_local = cur.fetchone()['id']

        for con in contactos_input:
            cur.execute(
                "INSERT INTO contacto (medico_id, nome) VALUES (%s, %s) RETURNING id",
                (medico_id_local, con.get('nome'))
            )
            c_id = cur.fetchone()['id']
            
            for t in con.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (contacto_id, medico_id, tipo, valor) VALUES (%s, %s, %s, %s)",
                    (c_id, medico_id_local, t.get('tipo'), t.get('valor'))
                )
            
            e_obj = con.get('endereco')
            if e_obj and isinstance(e_obj, dict):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (c_id, e_obj.get('tipo'), e_obj.get('valor'))
                )

        conn.commit()

        hapi_url = f"{FHIR_SERVER_URL}/Practitioner"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                cur.execute("UPDATE medicos SET fhir_id = %s WHERE id = %s", (str(fhir_id_gerado), medico_id_local))
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
        ref_paciente_local = data.get('refer_paciente', '')
        id_paciente_sql = int(ref_paciente_local.split('/')[-1]) if '/' in ref_paciente_local else None

        ref_medico_local = data.get('refer_medico', '')
        id_medico_sql = int(ref_medico_local.split('/')[-1]) if '/' in ref_medico_local else None

        if not id_paciente_sql:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida.")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (id_paciente_sql,))
        row_p = cur.fetchone()
        if not row_p or not row_p['fhir_id']:
            raise HTTPException(status_code=400, detail="Paciente local não sincronizado com HAPI.")
        fhir_id_paciente = row_p['fhir_id']

        fhir_id_medico = None
        if id_medico_sql:
            cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (id_medico_sql,))
            row_m = cur.fetchone()
            if row_m and row_m['fhir_id']:
                fhir_id_medico = row_m['fhir_id']

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

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Encounter")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Encounter: {mensagem}")

        try:
            cur.execute(
                """INSERT INTO consultas (paciente_id, medico_id, data_consulta, tipo_consulta) 
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (id_paciente_sql, id_medico_sql, data.get('data_consulta'), data.get('tipo_consulta'))
            )
            consulta_id_local = cur.fetchone()['id']

            cur.execute(
                "INSERT INTO historico (paciente_id, consulta_id) VALUES (%s, %s)",
                (id_paciente_sql, consulta_id_local)
            )
            conn.commit()
        except Exception as sql_err:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Erro ao gravar no SQL local: {str(sql_err)}")

        hapi_url = f"{FHIR_SERVER_URL}/Encounter"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                cur.execute("UPDATE consultas SET fhir_id = %s WHERE id = %s", (str(fhir_id_gerado), consulta_id_local))
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
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Paciente não existe no SQL")
        
        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Paciente sem sincronização HAPI")

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
        cur.execute("SELECT fhir_id FROM observacoes WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Observação não encontrada no SQL")
        
        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Observação sem mapeamento FHIR")

        hapi_url = f"{FHIR_SERVER_URL}/Observation/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "dados_provenientes_do_hapi": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

@app.get("/Observation")
async def get_patient_observations(patient: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (patient,))
        res_paciente = cur.fetchone()

        if not res_paciente or not res_paciente['fhir_id']:
            raise HTTPException(status_code=404, detail="Paciente não encontrado ou não sincronizado")

        fhir_patient_id = res_paciente['fhir_id']
        hapi_url = f"{FHIR_SERVER_URL}/Observation?patient={fhir_patient_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            fhir_data = response.json()
            observacoes = [entry["resource"] for entry in fhir_data.get("entry", [])]
            return {
                "id_local_paciente": patient,
                "id_fhir_paciente": fhir_patient_id,
                "total_observacoes": len(observacoes),
                "lista_observacoes": observacoes
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
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
        cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Médico não existe no SQL")

        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Médico não sincronizado")

        hapi_url = f"{FHIR_SERVER_URL}/Practitioner/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "recurso_fhir_do_servidor": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
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
        cur.execute("SELECT fhir_id FROM consultas WHERE id = %s", (consulta_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Consulta não encontrada no SQL")
        
        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Consulta sem fhir_id")

        hapi_url = f"{FHIR_SERVER_URL}/Encounter/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            return {
                "id_local": consulta_id,
                "id_fhir_no_hapi": fhir_id,
                "recurso_fhir_completo": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
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
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()
        
        if not result or not result['fhir_id']:
            raise HTTPException(status_code=404, detail="Paciente não encontrado ou sem ID FHIR")

        fhir_id = result['fhir_id']
        hapi_url = f"{FHIR_SERVER_URL}/Observation?subject=Patient/{fhir_id}"
        response = requests.get(hapi_url)

        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    finally:
        if conn: conn.close()