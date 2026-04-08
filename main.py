import io
import platform 
import pytesseract
from fastapi import FastAPI, UploadFile, File, HTTPException
from pdf2image import convert_from_bytes
import pdfplumber
import re
import gc
import uvicorn

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r"C:/Program Files/Tesseract-OCR/tesseract.exe"
    POPPLER_PATH = r"C:/poppler-25.12.0/Library/bin"
else:
   #linux
    POPPLER_PATH = None

app = FastAPI()

# SECCIÓN 1: PARSERS ESPECÍFICOS POR PROVEEDOR 

def parse_bayport_coa(page, lines):
    data = {
        "header_info": {},
        "shipped_to": [],
        "characteristics": []
    }
    
    # A. EXTRACCIÓN POR COORDENADAS (Zonas Basadas en Escaneo)

    rect_shipped_to = (0, 230, 300, 302) 
    
    rect_order_item = (309.6, 244.0, 357.6, 254.1)
    rect_customer_no = (309.6, 268.0, 357.6, 278.1)

    try:
        # Extraer Dirección Limpia
        addr_text = page.crop(rect_shipped_to).extract_text()
        if addr_text:
            
            data["shipped_to"] = [l.strip() for l in addr_text.split('\n') 
                                 if l.strip() and "SHIPPED TO" not in l]

        # Extraer Campos de la Derecha
        order_val = page.crop(rect_order_item).extract_text()
        data["header_info"]["order_item"] = order_val.strip() if order_val else "No encontrado"
        
        cust_val = page.crop(rect_customer_no).extract_text()
        data["header_info"]["customer_number"] = cust_val.strip() if cust_val else "No encontrado"
        
    except Exception as e:
        print(f"Error en recorte: {e}")
    
    # B. EXTRACCIÓN DE TABLA 
    v_lines = [0, 215, 285, 600] 
    rect_table = (0, 420, 600, 560) # Área de los datos de la tabla

    try:
        # Extraemos la tabla forzando las líneas verticales
        table = page.crop(rect_table).extract_table({
            "vertical_strategy": "explicit",
            "explicit_vertical_lines": v_lines,
            "horizontal_strategy": "text"
        })
        
        if table:
            for row in table:
                # Limpiamos cada celda 
                clean_row = [re.sub(r'[-_]{2,}', '', str(c)).strip() for c in row if c]
                
                # Solo procesamos si la fila tiene datos reales
                if len(clean_row) >= 2:
                    data["characteristics"].append({
                        "characteristic": clean_row[0],
                        "unit": clean_row[1] if len(clean_row) == 3 else "-",
                        "value": clean_row[-1]
                    })
    except: pass

    # B. LÓGICA DE LÍNEAS 
    for i, line in enumerate(lines):
        if "Date" == line and i + 1 < len(lines):
            data["header_info"]["date"] = lines[i+1]

        # Material Reference 
        if "Material: Our / Your reference" in line and i + 3 < len(lines):
            material_lines = lines[i+1 : i+4]
            data["header_info"]["material_our_/_Your_reference"] = " ".join(material_lines)
        
        # Batch / Quantity / Railcar 
        if "Batch" in line and "Quantity" in line:
            batch = re.search(r'Batch\s+([A-Z0-9]+)', line)
            qty = re.search(r'Quantity\s+([\d,]+\s+LB)', line)
            railcar = re.search(r'Railcar\s+([A-Z0-9]+)', line)
            

            if batch: data["header_info"]["batch"] = batch.group(1)
            if qty: data["header_info"]["quantity"] = qty.group(1)
            if railcar: data["header_info"]["railcar"] = railcar.group(1)
            
    return data


def parse_bayport_bol(lines):
    data = {
        "header_info": {},
        "ship_to": [],
        "product_details": {}
    }
    
    in_ship_to = False
    
    for line in lines:
        text = line.strip()
        if not text:
            continue
            
        # ---------------------------------------------------------
        # 1. EXTRACCIÓN DEL BLOQUE "SHIP TO" (Multilínea)
        # ---------------------------------------------------------
        if text.startswith("SHIP TO:"):
            in_ship_to = True
            # Guardamos la primera parte de la dirección quitando la etiqueta
            clean_first_line = text.replace("SHIP TO:", "").strip()
            if clean_first_line:
                data["ship_to"].append(clean_first_line)
            continue
            
        # Condición de salida: Si encontramos la siguiente sección, apagamos la bandera
        if in_ship_to and any(text.startswith(kw) for kw in ["CUSTOMER P/O:", "OUR ORDER", "SHIP DATE:"]):
            in_ship_to = False
            
        if in_ship_to:
            data["ship_to"].append(text)
            continue

        # ---------------------------------------------------------
        # 2. EXTRACCIÓN DE ENCABEZADOS (Una o más variables por línea)
        # ---------------------------------------------------------
        if text.startswith("CUSTOMER P/O:"):
            data["header_info"]["customer_po"] = text.split("CUSTOMER P/O:")[-1].strip()
            
        elif text.startswith("OUR ORDER #"):
            # Usamos regex por si hay espacios extra antes o después de los dos puntos
            match = re.search(r'OUR ORDER #\s*:\s*(\d+)', text)
            if match:
                data["header_info"]["our_order"] = match.group(1)
                
        elif text.startswith("SHIP DATE:"):
            # Aquí vienen dos datos en la misma línea: "SHIP DATE: ... FREIGHT: ..."
            match_date = re.search(r'SHIP DATE:\s*([\d/]+)', text)
            if match_date:
                data["header_info"]["ship_date"] = match_date.group(1)
                
            if "FREIGHT:" in text:
                data["header_info"]["freight"] = text.split("FREIGHT:")[-1].strip()
                
        elif text.startswith("ESTIMATED DELIVERY DATE:"):
            data["header_info"]["est_delivery_date"] = text.split("DATE:")[-1].strip()
            
        elif text.startswith("B/L #:"):
            data["header_info"]["bl_number"] = text.split("B/L #:")[-1].strip()
            
        elif text.startswith("VIA:"):
            data["header_info"]["via"] = text.split("VIA:")[-1].strip()
            
        elif text.startswith("SEALS:"):
            data["header_info"]["seals"] = text.split("SEALS:")[-1].strip()

        # ---------------------------------------------------------
        # 3. EXTRACCIÓN DE PRODUCTO Y TABLA (Dinámico y Flexible)
        # ---------------------------------------------------------
        
        # Buscamos la línea que tiene la estructura: [Vehículo] [Lote] [Cantidad] LB
        # ^ indica el inicio de la línea
        # \s+ indica uno o más espacios en blanco separando los datos
        match_linea_producto = re.search(r'^([A-Z0-9]+)\s+([A-Z0-9]+)\s+([\d\.]+)\s*LB', text)
        
        if match_linea_producto:
            # group(1) atrapa la primera palabra completa (Vehículo)
            data["product_details"]["vehicle"] = match_linea_producto.group(1)
            
            # group(2) atrapa la segunda palabra completa (Batch)
            data["product_details"]["batch"] = match_linea_producto.group(2)
            
            # group(3) atrapa los números de la cantidad antes del "LB"
            data["product_details"]["quantity"] = match_linea_producto.group(3)

        # Buscamos la línea de pesos que contiene GROSS, TARE y NET (Este se queda igual)
        if "GROSS:" in text and "NET:" in text:
            gross_m = re.search(r'GROSS:\s*([\d,]+)', text)
            tare_m = re.search(r'TARE:\s*([\d,]+)', text)
            net_m = re.search(r'NET:\s*([\d,\.]+)', text)
            
            if gross_m: data["product_details"]["gross_weight"] = gross_m.group(1)
            if tare_m: data["product_details"]["tare_weight"] = tare_m.group(1)
            if net_m: data["product_details"]["net_weight"] = net_m.group(1)
            
    return data

def parse_equistar_bol(lines):
    full_text = "\n".join(lines)
    data = {
        "header_info": {},
        "consignee": [],
        "send_freight_to": [],
        "materials": []
    }

    # 1. ENCABEZADO (Totalmente Dinámico por Posición)
    # \S+ captura cualquier cadena sin espacios. La fecha acepta letras, números, guiones y diagonales.
    header_pattern = r'^(\S+)\s+(\S+)\s+([\d\-/A-Za-z]+)\s+(\S+)\s+(\S+)\s+(.*)$'
    
    for i, line in enumerate(lines):
        # Buscamos la fila de los títulos
        if "BILL OF LADING NO" in line and "SALES ORDER NO" in line:
            # Revisamos las siguientes 1 o 2 líneas para extraer los valores
            for j in range(1, 3):
                if i + j < len(lines):
                    data_line = lines[i+j].strip()
                    # Ignorar si es una línea vacía o el siguiente encabezado
                    if not data_line or "CARRIER" in data_line:
                        continue
                    
                    match = re.search(header_pattern, data_line)
                    if match:
                        data["header_info"] = {
                            "bill_of_lading": match.group(1), 
                            "sales_order": match.group(2),    
                            "shipping_date": match.group(3),  
                            "ship_to_id": match.group(4),     
                            "vehicle_id": match.group(5),     
                            "customer_po": match.group(6).strip() 
                        }
                        break 
            
            if data["header_info"]: 
                break 

    # Si por alguna razón el OCR no encontró el encabezado, evitamos error inicializando el dict
    if "header_info" not in data or not data["header_info"]:
        data["header_info"] = {}

    # 2. SEPARACIÓN DE DIRECCIONES 
    is_address = False
    for line in lines:
        if "CONSIGNEE" in line and "SEND FREIGHT" in line:
            is_address = True
            continue
        if is_address:
            if any(x in line for x in ["Section 7", "Carrier Instructions", "Pkes"]):
                is_address = False
                continue
            
            # Volvemos a las anclas probadas que no rompen nombres como 'INC'
            parts = re.split(r'(EQUISTAR|PO Box|HOUSTON TX)', line, maxsplit=1)
            
            if len(parts) > 1:
                data["consignee"].append(parts[0].strip())
                data["send_freight_to"].append("".join(parts[1:]).strip())
            elif line.strip():
                data["consignee"].append(line.strip())

    data["consignee"] = list(dict.fromkeys(filter(None, data["consignee"])))
    data["send_freight_to"] = list(dict.fromkeys(filter(None, data["send_freight_to"])))

    # 3. EMBARGOS, PERMISOS Y FECHAS 
    delivery = re.search(r'Delivery date\s*:\s*([\d\-/A-Za-z]+)', full_text) 
    embargo = re.search(r'EMBARGO NUMBER:\s*([A-Z0-9]+?)(?=EMBARGO|PERMIT|$)', full_text) 
    permit = re.search(r'PERMIT NUMBER:\s*([A-Z0-9]+)', full_text) 
    
    data["header_info"]["delivery_date"] = delivery.group(1) if delivery else "N/A"
    data["header_info"]["embargo_no"] = embargo.group(1) if embargo else "N/A"
    data["header_info"]["permit_no"] = permit.group(1) if permit else "N/A"

    # 4. PESOS, LOTE Y MATERIAL 
    gross = re.search(r'Gross\s*Weight:\s*([\d,]+)', full_text) 
    tare = re.search(r'Tare\s*Weight:\s*([\d,]+)', full_text)   
    seal = re.search(r'Seal\s*Numbers:\s*(\d+)', full_text)     
    
    # Net Weight: Busca número antes de LBS o L BS
    net_match = re.search(r'([\d,]{4,})\s*(?:L\s*BS|LBS)', full_text)
    if not net_match:
        net_match = re.search(r'TOTALin\s*LBS\s*([\d,]+)', full_text)

    # Lote: Busca serie alfanumérica cerca del peso
    lot = re.search(r'\b([A-Z0-9]{7,15})\s+[\d,]+\s*(?:L\s*BS|LBS)', full_text) 

    # Descripción: Busca el valor después de 
    desc_match = re.search(r'NMFC:[^,\n]+,?\s*\n?(.*?)(?:, NON_REG|\n)', full_text)

    data["materials"].append({
        "description": desc_match.group(1).strip() if desc_match else "Material Desconocido",
        "lot_number": lot.group(1) if lot else "N/A",
        "net_weight": net_match.group(1) if net_match else "N/A",
        "gross_weight": gross.group(1) if gross else "N/A",
        "tare_weight": tare.group(1) if tare else "N/A",
        "seal_number": seal.group(1) if seal else "N/A"
    })

    return data

def parse_equistar_coa(page, lines):
    full_text = "\n".join(lines)
    data = {
        "header_info": {
            "product_name": None,
            "batch_number": None,
            "vehicle_number": None,
            "estimated_quantity": None,
            "material_number": None,
            "customer_order_no": None,
            "customer_number": None,
            "date_shipped": None,
            "sales_order_no": None,
            "delivery_item_no": None
        },
        "addresses": {
            "ship_to": [],
            "coa_contact": []
        },
        "properties": []
    }

    # 1. EXTRACCIÓN DE METADATOS (Manejo de columnas mezcladas) 
    # Usamos patrones que cortan la línea donde empieza la etiqueta de la derecha
    patterns = {
        "product_name": r"Product Name\s*:\s*(.*?)(?=\s*Customer Order No|$)",
        "batch_number": r"Batch Number\s*:\s*(.*?)(?=\s*Customer Number|$)",
        "vehicle_number": r"Vehicle Number\s*:\s*(.*?)(?=\s*Date Shipped|$)",
        "estimated_quantity": r"Estimated Quantity\s*:\s*(.*?)(?=\s*Sales Order No|$)",
        "material_number": r"Material Number\s*:\s*(.*?)(?=\s*Delivery Item No|$)",
        "customer_order_no": r"Customer Order No\.\s*:\s*(.*)",
        "customer_number": r"Customer Number\s*:\s*(.*)",
        "date_shipped": r"Date Shipped\s*:\s*(.*)",
        "sales_order_no": r"Sales Order No\.\s*:\s*(.*)",
        "delivery_item_no": r"Delivery Item No\.\s*:\s*(.*)"
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            # Limpiamos posibles espacios múltiples del OCR 
            data["header_info"][key] = match.group(1).strip()

    # 2. EXTRACCIÓN DE DIRECCIONES (Desduplicación de texto) 
    # En el COA, las direcciones aparecen pegadas como "MEXICO MEXICO"
    start_collecting = False
    for line in lines:
        if "Certificate of Analysis Contact" in line:
            start_collecting = True
            continue
        
        if "Product Name" in line or "____" in line:
            start_collecting = False
            break
            
        if start_collecting:
            # Separamos la línea por la mitad si el OCR duplicó el texto
            parts = re.split(r'\s{2,}', line.strip())
            if len(parts) >= 2:
                data["addresses"]["coa_contact"].append(parts[0].strip())
                data["addresses"]["ship_to"].append(parts[1].strip())
            elif len(parts) == 1:
                mid = len(line) // 2
                left, right = line[:mid].strip(), line[mid:].strip()
                if left == right:
                    data["addresses"]["coa_contact"].append(left)
                    data["addresses"]["ship_to"].append(right)
                else:
                    data["addresses"]["coa_contact"].append(line.strip())

   
    # Captura la fecha ignorando firmas al final
    for line in lines:
        if "Print Date:" in line:
            m_print = re.search(r'Print Date:\s*(.*?)(?=\s*[A-Z]{3,}|$)', line)
            if m_print: data["header_info"]["print_date"] = m_print.group(1).strip()


    # 3. EXTRACCIÓN DE TABLA POR COORDENADAS 
    words = page.extract_words()
    
    COL_RESULT = 190   
    COL_MIN = 260      
    COL_MAX = 330      
    COL_UNIT = 400     
    COL_METHOD = 480   

    # LÍMITES VERTICALES DINÁMICOS
    Y_INICIO_TABLA = 378 
    Y_FIN_TABLA = 9999 
    
    for w in words:
        txt = w['text'].upper()
        # Muro dinámico para frenar antes del texto inferior
        if "DELIVERY" in txt or "PRINT" in txt:
            if w['top'] > 450:
                Y_FIN_TABLA = min(Y_FIN_TABLA, w['top'] - 5)
                
    if Y_FIN_TABLA == 9999: Y_FIN_TABLA = 700 

    # Agrupación por renglones
    rows = []
    current_row = []
    last_y = -1

    sorted_words = sorted(words, key=lambda x: x['top'])
    for w in sorted_words:
        y = w['top']
        if Y_INICIO_TABLA < y < Y_FIN_TABLA:
            if last_y == -1 or abs(y - last_y) <= 4:
                current_row.append(w)
            else:
                rows.append(current_row)
                current_row = [w]
            last_y = y
    if current_row: rows.append(current_row)

    for row_words in rows:
        row_words = sorted(row_words, key=lambda x: x['x0'])
        line_text = " ".join([w['text'] for w in row_words])
        
        # Filtramos el encabezado de la tabla
        if len(line_text.strip()) > 5 and "Description" not in line_text:
            
            prop = {"description": "", "result": "", "min": "", "max": "", "unit": "", "test_method": ""}
            
            for w in row_words:
                x = w['x0']
                txt = w['text']
                
                # Asignación a columnas
                if x < COL_RESULT: prop["description"] += " " + txt
                elif COL_RESULT <= x < COL_MIN: prop["result"] += " " + txt
                elif COL_MIN <= x < COL_MAX: prop["min"] += " " + txt
                elif COL_MAX <= x < COL_UNIT: prop["max"] += " " + txt
                elif COL_UNIT <= x < COL_METHOD:
                    if re.match(r'A-\d+', txt): prop["test_method"] += " " + txt
                    else: prop["unit"] += " " + txt
                elif x >= COL_METHOD: prop["test_method"] += " " + txt

            res = prop["result"].strip()
            desc = prop["description"].strip()
            
            
            if res:
                # Flujo normal para datos limpios (Vehicle ID, Density, etc.)
                data["properties"].append({
                    "description": desc,
                    "result": res,
                    "min": prop["min"].strip(),
                    "max": prop["max"].strip(),
                    "unit": prop["unit"].strip(),
                    "test_method": prop["test_method"].strip()
                })
            else:
                # Si falló la asignación por columnas, limpia los guiones y forza el guardado
                clean_line = line_text.replace('_', '')
                clean_line = re.sub(r'\s+', ' ', clean_line).strip()
                
                # Intentamos separar los valores matemáticos de la línea limpia
                m = re.search(r'(.*?)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(.*?)\s+(A-\d+.*)', clean_line)
                
                if m:
                    # Si la regex logra separarlos, los acomodamos
                    data["properties"].append({
                        "description": m.group(1).strip(),
                        "result": m.group(2).strip(),
                        "min": m.group(3).strip(),
                        "max": m.group(4).strip(),
                        "unit": m.group(5).strip(),
                        "test_method": m.group(6).strip()
                    })
                else:
                    # Si de plano es ilegible, aventamos el texto completo para que no se pierda
                    data["properties"].append({
                        "description": clean_line,
                        "result": "RAW_DATA",
                        "min": "",
                        "max": "",
                        "unit": "",
                        "test_method": ""
                    })

    return data



def parse_nova_bol(page):
   
    data = {
        "header_info": {
            "ship_date": None,
            "shipper_number": None,
            "freight_terms": None,
            "loading_date_time": None,
            "final_destination": None,
            "required_delivery": None,
            "customer_order": None,
            "route": None,             
            "carrier": None,           
            "trailer_tank_no": None,   
            "seal_numbers": None       
        },
        "consignee": {
            "name": None,              
            "address": []
        },
        "shipper": {
            "name": "NOVA CHEMICALS CORPORATION", 
            "address": []
        },
        "mail_invoice_to": {
            "name": None,              
            "address": []
        },
        "emergency_contact": [],
        "customs_broker_forwarder": [],
        "materials": []
    }

    words = page.extract_words()
    full_text = page.extract_text()
    
    # Ordenamos de arriba hacia abajo y de izquierda a derecha
    sorted_words = sorted(words, key=lambda w: (round(w['top'], 1), w['x0']))
    
    invoice_address_lines = {}
    emergency_lines = {}
    customs_lines = {}
    cons_address_lines = {}

    for w in sorted_words:
        x = w['x0']
        y = w['top']
        txt = w['text']
        
        # 1. SHIP DATE (Coord: X ~360, Y ~70)
        if 350 < x < 420 and 65 < y < 75:
            data["header_info"]["ship_date"] = txt

        # 2. SHIPPER'S NUMBER (Coord: X ~475, Y ~70)
        if x > 450 and 65 < y < 75:
            data["header_info"]["shipper_number"] = txt

        # 3. FREIGHT TERMS (Coord: X > 360, Y ~104)
        if x > 360 and 100 < y < 110:
            if not data["header_info"]["freight_terms"]:
                data["header_info"]["freight_terms"] = txt
            else:
                data["header_info"]["freight_terms"] += " " + txt

        # 4. MAIL FREIGHT INVOICE TO (Coord: X > 350, Y de 124 a 160)
        if x > 350 and 120 < y < 170:
            y_rounded = round(y, 1)
            if y_rounded not in invoice_address_lines:
                invoice_address_lines[y_rounded] = []
            invoice_address_lines[y_rounded].append(txt)

        # 5. EMERGENCY CONTACT (Coord: X > 350, Y de 26 a 60)
        if x > 350 and 20 < y < 60:
            y_rounded = round(y, 1)
            if y_rounded not in emergency_lines:
                emergency_lines[y_rounded] = []
            emergency_lines[y_rounded].append(txt)

        # 6. LOADING DATE/TIME (Coord: X > 350, Y de 87 a 91)
        if x > 350 and 85 < y < 95:
            if not data["header_info"]["loading_date_time"]:
                data["header_info"]["loading_date_time"] = txt
            else:
                data["header_info"]["loading_date_time"] += " " + txt

        # 7. FINAL DESTINATION (Coord: X entre 350 y 420, Y ~190)
        if 350 < x < 420 and 185 < y < 195:
            if not data["header_info"]["final_destination"]:
                data["header_info"]["final_destination"] = txt
            else:
                data["header_info"]["final_destination"] += " " + txt

        # 8. REQUIRED DELIVERY (Coord: X > 430, Y ~190)
        if x > 430 and 185 < y < 195:
            if not data["header_info"]["required_delivery"]:
                data["header_info"]["required_delivery"] = txt
            else:
                data["header_info"]["required_delivery"] += " " + txt

        # 9. CUSTOMS BROKER / FORWARDER (Coord: X > 350, Y de 210 a 245)
        if x > 350 and 205 < y < 245:
            y_rounded = round(y, 1)
            if y_rounded not in customs_lines:
                customs_lines[y_rounded] = []
            customs_lines[y_rounded].append(txt)

        # 10. CUSTOMER ORDER NUMBER (Coord: X > 350, Y ~262)
        if x > 350 and 255 < y < 268:
            if not data["header_info"]["customer_order"]:
                data["header_info"]["customer_order"] = txt
            else:
                data["header_info"]["customer_order"] += " " + txt

        # 11. CONSIGNEE (Coord: X < 350, Y de 115 a 205)
        if x < 350 and 115 < y < 205:
            y_rounded = round(y, 1)
            if y_rounded not in cons_address_lines:
                cons_address_lines[y_rounded] = []
            cons_address_lines[y_rounded].append(txt)

        # 12. DESTINATION & ROUTE (Coord: X < 350, Y de 225 a 250)
        
        if x < 350 and 225 < y < 250:
            if not data["header_info"]["route"]:
                data["header_info"]["route"] = txt
            else:
                data["header_info"]["route"] += " " + txt

        # 13. NAME OF CARRIER (Coord: X < 350, Y de 275 a 285)
        if x < 350 and 275 < y < 285:
            if not data["header_info"]["carrier"]:
                data["header_info"]["carrier"] = txt
            else:
                data["header_info"]["carrier"] += " " + txt

        # 14. TRAILER / TANK CAR (Coord: X < 200, Y de 300 a 315)
        if x < 200 and 300 < y < 315:
            if not data["header_info"]["trailer_tank_no"]:
                data["header_info"]["trailer_tank_no"] = txt
            else:
                data["header_info"]["trailer_tank_no"] += txt

        # 15. SEAL NUMBERS (Coord: X > 200, Y de 305 a 335)
        if x > 200 and 305 < y < 335:
            
            if "seal_numbers" not in data["header_info"]: 
                data["header_info"]["seal_numbers"] = ""
                
            if not data["header_info"]["seal_numbers"]:
                data["header_info"]["seal_numbers"] = txt
            else:
                data["header_info"]["seal_numbers"] += " " + txt

    # 1. Extracción de identificadores del material
    m_batch = re.search(r"Batch:\s*([A-Z0-9]+)", full_text)
    m_order_item = re.search(r"Order/item:\s*([\d/]+)", full_text)
    m_rail_contract = re.search(r"Rail Contract #:\s*(.*?)(?=\n|$)", full_text)
    
    # Código arancelario 
    m_hs_code = re.search(r"(\d{4}\.\d{2}\.\d{2}\.\d{2})", full_text)
    
    # 2. Extracción de pesos
    m_gross = re.search(r"GROSS\s*([\d,]+)", full_text)
    m_tare = re.search(r"TARE\s*([\d,]+)", full_text)
    m_nets = re.findall(r"NET\s*([\d,]+)", full_text) 

    # 3. Guardado en la estructura
    data["materials"].append({
        "description": "POLYETHYLENE RESIN",
        "batch": m_batch.group(1).strip() if m_batch else "",
        "order_item": m_order_item.group(1).strip() if m_order_item else "",
        "rail_contract": m_rail_contract.group(1).strip() if m_rail_contract else "",
        "hs_code": m_hs_code.group(1).strip() if m_hs_code else "",
        "net_weight": m_nets[-1].strip() if m_nets else "",
        "gross_weight": m_gross.group(1).strip() if m_gross else "",
        "tare_weight": m_tare.group(1).strip() if m_tare else ""
    })


    # Procesamos la dirección del Consignee
    for y_val in sorted(cons_address_lines.keys()):
        line_text = " ".join(cons_address_lines[y_val]).strip()
        if not data["consignee"]["name"]:
            data["consignee"]["name"] = line_text
        else:
            data["consignee"]["address"].append(line_text)
    
    # Limpiamos el texto de Loading Date (le quitamos las etiquetas para dejar solo la fecha/hora)
    if data["header_info"]["loading_date_time"]:
        loading_clean = data["header_info"]["loading_date_time"]
        # Eliminamos palabras innecesarias para quedarnos solo con "2026.02.02 / 00:00"
        loading_clean = re.sub(r'Loading|Date/Time:|Date|Time', '', loading_clean).strip()
        data["header_info"]["loading_date_time"] = loading_clean

    # Procesamos la dirección de facturación
    for y_val in sorted(invoice_address_lines.keys()):
        line_text = " ".join(invoice_address_lines[y_val]).strip()
        if not data["mail_invoice_to"]["name"]:
            data["mail_invoice_to"]["name"] = line_text
        else:
            data["mail_invoice_to"]["address"].append(line_text)

    # Procesamos los contactos de emergencia
    for y_val in sorted(emergency_lines.keys()):
        line_text = " ".join(emergency_lines[y_val]).strip()
        data["emergency_contact"].append(line_text)

    # Procesamos el agente aduanal
    for y_val in sorted(customs_lines.keys()):
        line_text = " ".join(customs_lines[y_val]).strip()
        data["customs_broker_forwarder"].append(line_text)

    return data


def parse_nova_coa(lines):
    data = {
        "header_info": {},
        "ship_to": [],
        "characteristics": []
    }

    in_table = False
    capture_ship_to = False

    for line in lines:
        text = line.strip()
        if not text:
            continue


        
        if "novachemicals.com" in text or "Certificate of Analysis" in text:
            capture_ship_to = True
            continue
            
        if text.startswith("Order No.:"):
            capture_ship_to = False

        if capture_ship_to:
            # Limpiamos la fecha que a veces el PDF pega al nombre del cliente (ej. "February 01, 2026")
            clean_address_line = re.sub(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{2},\s+\d{4}', '', text).strip()
            
            if clean_address_line and clean_address_line != "Page: 1 of 1":
                data["ship_to"].append(clean_address_line)

 
        if text.startswith("Order No.:"):
            
            parts = text.split("P.O.:")
            data["header_info"]["order_no"] = parts[0].replace("Order No.:", "").strip()
            if len(parts) > 1:
                data["header_info"]["po_number"] = parts[1].strip()

        elif text.startswith("Delivery No:"):
            data["header_info"]["delivery_no"] = text.replace("Delivery No:", "").strip()

        elif text.startswith("Railcar/Container:"):
            data["header_info"]["railcar"] = text.replace("Railcar/Container:", "").strip()

        elif text.startswith("Shipping Date:"):
            data["header_info"]["shipping_date"] = text.replace("Shipping Date:", "").strip()

        elif text.startswith("Batch:"):
            data["header_info"]["batch"] = text.replace("Batch:", "").strip()

        elif text.startswith("Product:"):
            data["header_info"]["product"] = text.replace("Product:", "").strip()

        elif text.startswith("Inspection Lot:"):
            data["header_info"]["inspection_lot"] = text.replace("Inspection Lot:", "").strip()

        elif text.startswith("Mnfg. Date:"):
            data["header_info"]["mnfg_date"] = text.replace("Mnfg. Date:", "").strip()

        elif text.startswith("Quantity:"):
            data["header_info"]["quantity"] = text.replace("Quantity:", "").strip()

        
        if text.startswith("Characteristic Unit Results"):
            in_table = True
            continue
        # Apagamos la bandera de la tabla cuando llegamos al pie de página
        elif in_table and (text.startswith("For Shipment") or text.startswith("Contact(s)")):
            in_table = False

        if in_table:
            # Separamos la línea por espacios
            parts = text.split()
            numeric_values = []
            
            # Extraemos todos los números (enteros o decimales) del final de la línea hacia atrás
            while parts and re.match(r'^[\d\.]+$', parts[-1]):
                numeric_values.insert(0, parts.pop())
                
            # Lo que queda en la lista 'parts' es el nombre de la característica y su unidad
            char_and_unit = " ".join(parts).strip()
            
            if char_and_unit and numeric_values:
                # Mapeamos los valores extraídos asumiendo el orden: [Result, Min, Max]
                result = numeric_values[0] if len(numeric_values) > 0 else None
                minimum = numeric_values[1] if len(numeric_values) > 1 else None
                maximum = numeric_values[2] if len(numeric_values) > 2 else None
                
                data["characteristics"].append({
                    "characteristic_and_unit": char_and_unit,
                    "result": result,
                    "minimum": minimum,
                    "maximum": maximum
                })

    return data

def parse_westlake_bol(page, lines):
    data = {
        "header_info": {},
        "consigned_to": [],
        "product_details": {}
    }


    if isinstance(lines, dict) and "full_line_list" in lines:
        lines = lines["full_line_list"]

    elif isinstance(lines, str):
        lines = lines.split('\n')

    capture_address = False

    for line in lines:
        text = str(line).strip()
        if not text:
            continue


        match_bl = re.search(r'B/L NO\.\s*(\d+)', text, re.IGNORECASE)
        if match_bl: data["header_info"]["bl_number"] = match_bl.group(1)

        match_sales = re.search(r'SALES ORDER:\s*([A-Z0-9-]+)', text, re.IGNORECASE)
        if match_sales: data["header_info"]["sales_order"] = match_sales.group(1)

        if "DATE" in text.upper() and "SHIPPER" in text.upper():
            match_date = re.search(r'DATE\s*([\d/]+)', text, re.IGNORECASE)
            if match_date: data["header_info"]["date"] = match_date.group(1)

        match_po = re.search(r'CONSIGNEES ORDER NO\.\s*(.+)', text, re.IGNORECASE)
        if match_po: data["header_info"]["customer_po"] = match_po.group(1).strip()

        match_inco = re.search(r'INCOTERMS\s*(.+)', text, re.IGNORECASE)
        if match_inco: data["header_info"]["incoterms"] = match_inco.group(1).strip()

        match_rail = re.search(r'RAILCAR#\s*([A-Z0-9]+)', text, re.IGNORECASE)
        if match_rail: data["header_info"]["railcar"] = match_rail.group(1)


        if "CONSIGNED TO" in text.upper():
            capture_address = True
            continue
            
        if capture_address and any(kw in text.upper() for kw in ["SHIP TO", "INCOTERMS", "ROUTE", "PICK UP"]):
            capture_address = False

        if capture_address:
            clean_line = text
            billing_keywords = [
                "WESTLAKE PETROCHEMICALS LLC", 
                "WESTLAKE CENTER", 
                "2801 POST OAK BLVD",
                "HOUSTON TX 77056", 
                "(PREPAID)EMAIL FREIGHT BILL TO", 
                "ACCOUNTSPAYABLE@WESTLAKE.COM",
                "Ste. 600",
                "MAIL FREIGHT BILL TO:"
            ]
            
            for kw in billing_keywords:
                split_line = re.split(re.escape(kw), clean_line, flags=re.IGNORECASE)
                clean_line = split_line[0].strip()

            if clean_line:
                data["consigned_to"].append(clean_line)


        match_gross = re.search(r'GROSS WT\.\(LB\)\s*([\d,]+)', text, re.IGNORECASE)
        if match_gross: data["product_details"]["gross_weight"] = match_gross.group(1)

        match_tare = re.search(r'TARE WT\.\(LB\)\s*([\d,]+)', text, re.IGNORECASE)
        if match_tare: data["product_details"]["tare_weight"] = match_tare.group(1)

        match_net = re.search(r'NET WT\.\(LB\)\s*([\d,]+)', text, re.IGNORECASE)
        if match_net: data["product_details"]["net_weight"] = match_net.group(1)

        match_seal = re.search(r'Seal#:\s*([A-Z0-9]+)', text, re.IGNORECASE)
        if match_seal: data["product_details"]["seal"] = match_seal.group(1)

        match_lot = re.search(r'Lot\s*#\s*([A-Z0-9]+)', text, re.IGNORECASE)
        if match_lot: data["product_details"]["lot"] = match_lot.group(1)

    return data

def parse_westlake_coa(all_text):
    data = {
        "header_info": {
            "customer_po": None,
            "order_number": None,
            "delivery_number": None,
            "date": None,
            "customer_number": None,
            "material_id": None,     
            "material_desc": None,   
            "safety_note": None,     
            "railcar": None,
            "batch": None,
            "quantity": None
        },
        "properties": []
    }

    # 1. ENCABEZADO
    
    # Customer PO (Captura todo antes de la fecha /)
    m_po = re.search(r"Customer PO item/date\s*\n.*?\s\s+([A-Z0-9\s-]+)(?=\s*/)", all_text, re.IGNORECASE)
    if m_po: 
        data["header_info"]["customer_po"] = m_po.group(1).strip()
    else:
        # Respaldo por si no hay doble espacio: buscamos el patrón IAME directamente
        m_po_fallback = re.search(r"(IAME\s*OC-[\d-]+)", all_text)
        if m_po_fallback: data["header_info"]["customer_po"] = m_po_fallback.group(1).strip()
        
    # Order Number
    m_order = re.search(r"Order item/date\s*\n\s*(.*?)(?=\s*/|$)", all_text, re.IGNORECASE)
    if m_order: data["header_info"]["order_number"] = m_order.group(1).strip()

    # Delivery Number 
    m_delivery = re.search(r"Delivery item/date\s*\n\s*(\d{8}\s+\d{6}\s*/\s*\d{2}/\d{2}/\d{4})", all_text)
    if m_delivery: data["header_info"]["delivery_number"] = m_delivery.group(1).strip()

    # Date y Customer Number
    m_date = re.search(r"Date\s*\n(\d{2}/\d{2}/\d{4})", all_text)
    if m_date: data["header_info"]["date"] = m_date.group(1).strip()

    m_cust_no = re.search(r"Customer number\s*\n(\d+)", all_text)
    if m_cust_no: data["header_info"]["customer_number"] = m_cust_no.group(1).strip()

    # 2. BLOQUE DE MATERIAL Y TRANSPORTE 
    
    # ID de Material (el número justo debajo de MATERIAL:)
    m_mat_id = re.search(r"MATERIAL:\s*\n(\d+)", all_text)
    if m_mat_id: data["header_info"]["material_id"] = m_mat_id.group(1).strip()
    
    # Descripción de Material (la línea técnica debajo del ID)
    m_mat_desc = re.search(r"Material\s+([A-Z0-9, ]+)", all_text)
    if m_mat_desc: data["header_info"]["material_desc"] = m_mat_desc.group(1).strip()

    # Nota de seguridad
    m_note = re.search(r"Note:\s*(.*?)(?=\n|$)", all_text)
    if m_note: data["header_info"]["safety_note"] = m_note.group(1).strip()

    # Vagón, Lote y Cantidad
    m_railcar = re.search(r"Railcar\s+([A-Z0-9]+)", all_text)
    if m_railcar: data["header_info"]["railcar"] = m_railcar.group(1).strip()

    m_batch = re.search(r"Batch\s+([0-9]+)", all_text)
    if m_batch: data["header_info"]["batch"] = m_batch.group(1).strip()

    m_qty = re.search(r"Quantity\s+([\d,.]+)\s*(LB|KG)?", all_text)
    if m_qty: data["header_info"]["quantity"] = f"{m_qty.group(1).strip()} {m_qty.group(2) or ''}".strip()

    # 3. TABLA DE PROPIEDADES 
    
    prop_matches = re.findall(r"(Melt Index|Density|Slip|Anitblock|Antiblock)\s+([A-Za-z0-9/]+)\s+([\d.]+)", all_text, re.IGNORECASE)
    
    for match in prop_matches:
    
        desc = "Antiblock" if match[0].lower() == "anitblock" else match[0] 
        data["properties"].append({
            "description": desc.strip(),
            "unit": match[1].strip(),
            "value": match[2].strip()  
        })

    return data

# SECCIÓN 2: SELECTOR DE PROCESO 
def get_parser_type(filename, lines):
    """Identificación por prefijo exacto en el nombre (2 palabras), con respaldo de texto"""
    
    name = filename.lower().replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
    words = name.split()
    
    if len(words) >= 2:
        prefix = f"{words[0]}_{words[1]}"
        tipos_validos = {
            "bayport_bol", "bayport_coa",
            "equistar_bol", "equistar_coa",
            "nova_bol", "nova_coa",
            "westlake_bol", "westlake_coa"
        }
        if prefix in tipos_validos:
            return prefix

        
    return "unknown"

#  SECCIÓN 3: ENDPOINT PRINCIPAL 

@app.post("/clean-pdf")
async def clean_pdf(file: UploadFile = File(...)):
    contents = None
    images = None
    first_page = None
    
    try:
        contents = await file.read()
        all_text = ""
        method_used = "Direct"
        

        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            if len(pdf.pages) > 0:
                first_page = pdf.pages[0] 
                
            for p in pdf.pages:
                text = p.extract_text()
                if text: 
                    all_text += text + "\n"
        
        # DETECCIÓN DE "PDF IMAGEN" -> ACTIVAR OCR
        if not all_text.strip():
            method_used = "OCR"
            images = convert_from_bytes(contents, poppler_path=POPPLER_PATH)
            for img in images:
                all_text += pytesseract.image_to_string(img, lang='spa+eng') + "\n"
                img.close() 
            
            del images 

        lines = [l.strip() for l in all_text.replace('\xa0', ' ').split('\n') if l.strip()]
        
        if not lines:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo")

        parser_type = get_parser_type(file.filename, lines)
        
        bol_permitidos_ocr = ["bayport_bol", "equistar_bol", "nova_bol", "westlake_bol", "unknown"]
        if method_used == "OCR" and parser_type not in bol_permitidos_ocr:
            raise HTTPException(
                status_code=400, 
                detail=f"Formato inválido. El documento {parser_type.upper()} requiere un PDF original de texto, no un documento escaneado/imagen."
            )
        
        if parser_type == "bayport_bol":
            structured_data = parse_bayport_bol(lines)
        elif parser_type == "bayport_coa":
            structured_data = parse_bayport_coa(first_page, lines)
        elif parser_type == "equistar_bol":
            structured_data = parse_equistar_bol(lines)
        elif parser_type == "equistar_coa":
            structured_data = parse_equistar_coa(first_page, lines)
        elif parser_type == "nova_bol":
            structured_data = parse_nova_bol(first_page)
        elif parser_type == "nova_coa":
            structured_data = parse_nova_coa(lines)
        elif parser_type == "westlake_bol":
            structured_data = parse_westlake_bol(first_page, lines)
        elif parser_type == "westlake_coa":
            structured_data = parse_westlake_coa(all_text)
        else:
            raise HTTPException(status_code=400, detail="Proveedor no reconocido")

        return {
            "status": "success",
            "method": method_used,
            "provider": parser_type,
            "extracted_data": structured_data,
            "full_line_list": lines
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    finally:
        # 6. LIMPIEZA FORZADA DE MEMORIA 
        del contents
        del first_page
        
        # Forzamos al sistema a barrer la basura de la RAM en este instante
        gc.collect()



if __name__ == "__main__":
    
    uvicorn.run(app, host="0.0.0.0", port=8000)