-- 1. Tabela Principal de Pacientes
CREATE TABLE pacientes (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(255) NOT NULL,
    genero CHAR(1), -- 'm', 'f', 'other'
    data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Tabela para Telefones/Emails (Telecom)
-- Relaciona-se com o paciente (1 para N)
CREATE TABLE paciente_telecom (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES pacientes(id) ON DELETE CASCADE,
    tipo VARCHAR(50), -- 'telemóvel', 'email'
    valor VARCHAR(255)
);

-- 3. Tabela de Contactos de Emergência
CREATE TABLE paciente_contactos (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES pacientes(id) ON DELETE CASCADE,
    nome_contacto VARCHAR(255),
    relacao VARCHAR(50)
);

-- 4. Tabela de Observações (Baseada no teu exemplo de Oxigénio)
CREATE TABLE observacoes (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES pacientes(id),
    estado VARCHAR(50), -- 'final'
    codigo_loinc VARCHAR(20), -- '59408-5'
    codigo_display VARCHAR(100), -- 'Oxygen saturation'
    data_execucao TIMESTAMP,
    valor_medicao FLOAT,
    unidade_medicao VARCHAR(10) -- '%'
);