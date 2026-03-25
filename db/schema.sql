-- schema.sql

CREATE TABLE IF NOT EXISTS patients (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(255) NOT NULL,
    genero CHAR(1)
);

CREATE TABLE IF NOT EXISTS contacto (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    nome VARCHAR(255)
);

-- Note que esta já inclui a coluna contacto_id que faltava
CREATE TABLE IF NOT EXISTS telecom (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    contacto_id INT REFERENCES contacto(id) ON DELETE CASCADE,
    tipo VARCHAR(50),
    valor VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS endereco (
    id SERIAL PRIMARY KEY,
    contacto_id INT REFERENCES contacto(id) ON DELETE CASCADE,
    tipo VARCHAR(50),
    valor TEXT
);

CREATE TABLE IF NOT EXISTS observacoes (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    estado VARCHAR(50)
);