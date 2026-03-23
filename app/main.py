from fastapi import FastAPI, HTTPException
import database  

app = FastAPI()

# 1. GET Patient por ID 
@app.get("/Patient/{patient_id}")
async def read_patient(patient_id: int):
    # Vai à BD buscar os dados da tabela SQL
    patient_data = database.get_patient_from_sql(patient_id)
    if not patient_data:
        raise HTTPException(status_code=404, detail="Patient not found")
    

    return {
        "resourceType": "Patient",
        "id": str(patient_data["id"]),
        "name": [{"family": patient_data["apelido"], "given": [patient_data["nome"]]}]
    }

# 2. POST Patient (Criar novo) 
@app.post("/Patient")
async def create_patient(patient_fhir: dict):
    nome = patient_fhir['name'][0]['given'][0]
    apelido = patient_fhir['name'][0]['family']
    
    new_id = database.insert_patient_sql(nome, apelido)
    return {"message": "Paciente criado", "id": new_id}

# 3. GET Observation por ID de Paciente 
@app.get("/Observation")
async def read_observations(patient: str):
    obs_list = database.get_observations_by_patient(patient)
    
    return {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Observation", "valueQuantity": {"value": o['valor']}}} 
            for o in obs_list
        ]
    }