"""
=============================================================================
RECOLECTOR DIARIO DE LA RED METEOROLÓGICA ANACAFE — INAB / SIG-INAB
=============================================================================
Descarga todas las estaciones activas de la red meteorológica de ANACAFE,
calcula métricas diarias/48h, genera capas vectoriales (GeoJSON y GeoPackage .gpkg)
y tablas CSV para almacenamiento permanente e histórico en ownCloud (05_ESTACIONES).
=============================================================================
Uso:
    python harvest_anacafe_daily.py
=============================================================================
"""

import os
import sys
import json
import sqlite3
import struct
import datetime
import requests
import concurrent.futures

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ANACAFE_CATALOG_URL = 'https://meteorologia.anacafe.org/servicios/api.php?op=estaciones'
ANACAFE_OBS_URL = 'https://meteorologia.anacafe.org/servicios/api.php?op=obs'

def harvest_anacafe(output_dir="."):
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime('%Y-%m-%d')
    d_yesterday = (now - datetime.timedelta(days=2)).strftime('%Y-%m-%d')

    print(f"[{date_str}] Consultando catálogo de estaciones de ANACAFE...")
    try:
        r = requests.get(ANACAFE_CATALOG_URL, timeout=15)
        raw_catalog = r.json()
        stations = raw_catalog if isinstance(raw_catalog, list) else raw_catalog.get('value', [])
        print(f"✓ {len(stations)} estaciones identificadas en la red de ANACAFE.")
    except Exception as e:
        print(f"Error al obtener catálogo: {e}")
        return None

    def fetch_station(s):
        sid = str(s.get('IdEstacion'))
        sname = str(s.get('Nombre', f'Estacion {sid}'))
        try:
            lat = float(s.get('Latitud'))
            lon = float(s.get('Longitud'))
        except (ValueError, TypeError):
            return None
        muni = str(s.get('Municipio', ''))

        url = f"{ANACAFE_OBS_URL}&id={sid}&fecha1={d_yesterday}&fecha2={date_str}"
        try:
            res = requests.get(url, timeout=10)
            if not res.ok:
                return None
            data = res.json()
            obs = data.get('obs', {})
            dias = obs.get('dia') or obs.get('fecha') or []
            horas = obs.get('hora') or []
            if not dias or not horas:
                return None

            def clean_float_list(arr):
                out = []
                if not arr: return out
                for x in arr:
                    try:
                        v = float(str(x).replace(',', '.'))
                        if not (v != v):
                            out.append(v)
                    except:
                        pass
                return out

            t_list = clean_float_list(obs.get('temperatura'))
            h_list = clean_float_list(obs.get('humedad'))
            p_list = clean_float_list(obs.get('lluvia'))
            w_list = clean_float_list(obs.get('vientoV'))
            rad_list = clean_float_list(obs.get('radiacion'))
            dew_list = clean_float_list(obs.get('rocio'))
            soil_list = clean_float_list(obs.get('suelo'))

            n = len(dias)
            readings = []
            for k in range(n):
                readings.append({
                    'id': sid, 'nombre': sname, 'muni': muni,
                    'lat': lat, 'lon': lon,
                    'fecha': dias[k], 'hora': horas[k] if k < len(horas) else '',
                    'temp': obs.get('temperatura', [])[k] if k < len(obs.get('temperatura', [])) else '',
                    'hr': obs.get('humedad', [])[k] if k < len(obs.get('humedad', [])) else '',
                    'lluvia': obs.get('lluvia', [])[k] if k < len(obs.get('lluvia', [])) else '',
                    'viento': obs.get('vientoV', [])[k] if k < len(obs.get('vientoV', [])) else '',
                    'rad': obs.get('radiacion', [])[k] if k < len(obs.get('radiacion', [])) else '',
                    'rocio': obs.get('rocio', [])[k] if k < len(obs.get('rocio', [])) else '',
                    'suelo': obs.get('suelo', [])[k] if k < len(obs.get('suelo', [])) else ''
                })

            return {
                'id': sid, 'nombre': sname, 'muni': muni,
                'lat': lat, 'lon': lon, 'registros': n,
                't_max': max(t_list) if t_list else None,
                't_min': min(t_list) if t_list else None,
                't_avg': sum(t_list)/len(t_list) if t_list else None,
                'h_avg': sum(h_list)/len(h_list) if h_list else None,
                'p_sum': sum(p_list) if p_list else 0.0,
                'w_max': max(w_list) if w_list else None,
                'rad_avg': sum(rad_list)/len(rad_list) if rad_list else None,
                'dew_avg': sum(dew_list)/len(dew_list) if dew_list else None,
                'soil_avg': sum(soil_list)/len(soil_list) if soil_list else None,
                'ultima_fecha': dias[-1] if dias else date_str,
                'ultima_hora': horas[-1] if horas else '',
                'readings': readings
            }
        except Exception:
            return None

    print("Descargando observaciones en paralelo (25 hilos concurrentes)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        results = list(executor.map(fetch_station, stations))

    active = [s for s in results if s is not None]
    print(f"✓ {len(active)} estaciones activas con datos transmitidos en las últimas 48h.")

    os.makedirs(output_dir, exist_ok=True)

    # 1. GeoJSON
    geojson_path = os.path.join(output_dir, f"ANACAFE_Red_Meteorologica_{date_str}.geojson")
    features = []
    for s in active:
        features.append({
            "type": "Feature",
            "geometry": { "type": "Point", "coordinates": [round(s['lon'], 5), round(s['lat'], 5)] },
            "properties": {
                "id_estacion": s['id'],
                "nombre": s['nombre'],
                "municipio": s['muni'],
                "fecha_corte": date_str,
                "ultima_lectura": f"{s['ultima_fecha']} {s['ultima_hora']}",
                "registros_48h": s['registros'],
                "temp_media_c": round(s['t_avg'], 1) if s['t_avg'] is not None else None,
                "temp_max_c": round(s['t_max'], 1) if s['t_max'] is not None else None,
                "temp_min_c": round(s['t_min'], 1) if s['t_min'] is not None else None,
                "humedad_rel_pct": round(s['h_avg'], 0) if s['h_avg'] is not None else None,
                "precipitacion_48h_mm": round(s['p_sum'], 1) if s['p_sum'] is not None else 0.0,
                "viento_max_kmh": round(s['w_max'], 1) if s['w_max'] is not None else None,
                "radiacion_media_wm2": round(s['rad_avg'], 0) if s['rad_avg'] is not None else None,
                "punto_rocio_c": round(s['dew_avg'], 1) if s['dew_avg'] is not None else None,
                "humedad_suelo_pct": round(s['soil_avg'], 1) if s['soil_avg'] is not None else None
            }
        })
    with open(geojson_path, 'w', encoding='utf-8') as f:
        json.dump({
            "type": "FeatureCollection",
            "crs": { "type": "name", "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" } },
            "features": features
        }, f, indent=2, ensure_ascii=False)
    print(f"✓ GeoJSON exportado: {geojson_path}")

    # 2. CSV
    csv_path = os.path.join(output_dir, f"ANACAFE_Mediciones_Completas_{date_str}.csv")
    csv_rows = ["ID_Estacion,Nombre,Municipio,Latitud,Longitud,Fecha,Hora,Temperatura_C,Humedad_Relativa_pct,Precipitacion_mm,Viento_kmh,Radiacion_wm2,Punto_Rocio_C,Humedad_Suelo_pct"]
    for s in active:
        for r in s.get('readings', []):
            csv_rows.append(f'"{r["id"]}","{r["nombre"]}","{r["muni"]}",{r["lat"]},{r["lon"]},"{r["fecha"]}","{r["hora"]}",{r["temp"]},{r["hr"]},{r["lluvia"]},{r["viento"]},{r["rad"]},{r["rocio"]},{r["suelo"]}')
    with open(csv_path, 'w', encoding='utf-8-sig') as f:
        f.write("\r\n".join(csv_rows))
    print(f"✓ CSV de mediciones exportado: {csv_path}")

    # 3. GeoPackage OGC Estándar
    gpkg_path = os.path.join(output_dir, f"ANACAFE_Estaciones_Meteorologicas_{date_str}.gpkg")
    if os.path.exists(gpkg_path):
        os.remove(gpkg_path)

    conn = sqlite3.connect(gpkg_path)
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA application_id = 1196444487;
        PRAGMA user_version = 10200;

        CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL,
            description TEXT
        );

        INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES 
        ('WGS 84 geodetic', 4326, 'EPSG', 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', 'WGS 84');

        CREATE TABLE IF NOT EXISTS gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY,
            data_type TEXT NOT NULL,
            identifier TEXT UNIQUE,
            description TEXT DEFAULT '',
            last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
            srs_id INTEGER,
            CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
        );

        CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL, m TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name)
        );

        CREATE TABLE IF NOT EXISTS estaciones_anacafe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            geom BLOB,
            id_estacion TEXT,
            nombre TEXT,
            municipio TEXT,
            latitud DOUBLE,
            longitud DOUBLE,
            fecha_corte TEXT,
            registros_48h INTEGER,
            temp_media_c DOUBLE,
            temp_max_c DOUBLE,
            temp_min_c DOUBLE,
            humedad_rel_pct DOUBLE,
            precipitacion_48h_mm DOUBLE,
            viento_max_kmh DOUBLE,
            radiacion_media_wm2 DOUBLE,
            punto_rocio_c DOUBLE,
            humedad_suelo_pct DOUBLE
        );
    """)

    def make_gpkg_point(lon, lat):
        header = struct.pack('<2sBBii', b'GP', 0, 1, 4326, 0)
        wkb = struct.pack('<BIdd', 1, 1, lon, lat)
        return header + wkb

    for s in active:
        cur.execute("""
            INSERT INTO estaciones_anacafe 
            (geom, id_estacion, nombre, municipio, latitud, longitud, fecha_corte, registros_48h,
             temp_media_c, temp_max_c, temp_min_c, humedad_rel_pct, precipitacion_48h_mm,
             viento_max_kmh, radiacion_media_wm2, punto_rocio_c, humedad_suelo_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            make_gpkg_point(s['lon'], s['lat']), s['id'], s['nombre'], s['muni'], s['lat'], s['lon'], date_str, s['registros'],
            round(s['t_avg'], 1) if s['t_avg'] is not None else None,
            round(s['t_max'], 1) if s['t_max'] is not None else None,
            round(s['t_min'], 1) if s['t_min'] is not None else None,
            round(s['h_avg'], 0) if s['h_avg'] is not None else None,
            round(s['p_sum'], 1) if s['p_sum'] is not None else 0.0,
            round(s['w_max'], 1) if s['w_max'] is not None else None,
            round(s['rad_avg'], 0) if s['rad_avg'] is not None else None,
            round(s['dew_avg'], 1) if s['dew_avg'] is not None else None,
            round(s['soil_avg'], 1) if s['soil_avg'] is not None else None
        ))

    cur.execute("""
        INSERT OR REPLACE INTO gpkg_contents 
        (table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id)
        VALUES ('estaciones_anacafe', 'features', 'estaciones_anacafe', 'Red Meteorológica ANACAFE Diaria',
                -92.25, 13.73, -88.22, 17.82, 4326)
    """)

    cur.execute("""
        INSERT OR REPLACE INTO gpkg_geometry_columns 
        (table_name, column_name, geometry_type_name, srs_id, z, m)
        VALUES ('estaciones_anacafe', 'geom', 'POINT', 4326, 0, 0)
    """)

    conn.commit()
    conn.close()
    print(f"✓ Geodatabase GeoPackage (.gpkg) exportada: {gpkg_path}")

    return {
        'geojson': geojson_path,
        'csv': csv_path,
        'gpkg': gpkg_path,
        'total_activas': len(active)
    }

if __name__ == '__main__':
    harvest_anacafe()
