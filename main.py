import io
import platform 
import pytesseract
from fastapi import FastAPI, UploadFile, File, HTTPException
from pdf2image import convert_from_bytes
import pdfplumber
import re

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r"C:/Program Files/Tesseract-OCR/tesseract.exe"
    POPPLER_PATH = r"C:/poppler-25.12.0/Library/bin"
else:
   #linux
    POPPLER_PATH = None

app = FastAPI()

# SECCIÓN 1: PARSERS ESPECÍFICOS POR PROVEEDOR ---

def parse_bayport(page, lines):
    data = {
        "header_info": {},
        "shipped_to": [],
        "characteristics": []
    }
    
    # A. EXTRACCIÓN POR COORDENADAS (Zonas Basadas en Escaneo) ---

    rect_shipped_to = (0, 230, 300, 302) 
    
    rect_order_item = (309.6, 244.0, 357.6, 254.1)
    rect_customer_no = (309.6, 268.0, 357.6, 278.1)

    try:
        # Extraer Dirección Limpia
        addr_text = page.crop(rect_shipped_to).extract_text()
        if addr_text:
            # Dividimos por líneas y limpiamos
            data["shipped_to"] = [l.strip() for l in addr_text.split('\n') 
                                 if l.strip() and "SHIPPED TO" not in l]

        # Extraer Campos de la Derecha
        order_val = page.crop(rect_order_item).extract_text()
        data["header_info"]["order_item"] = order_val.strip() if order_val else "No encontrado"
        
        cust_val = page.crop(rect_customer_no).extract_text()
        data["header_info"]["customer_number"] = cust_val.strip() if cust_val else "No encontrado"
        
    except Exception as e:
        print(f"Error en recorte: {e}")
    
    # B. EXTRACCIÓN DE TABLA (Usando tus carriles de columna) 
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
                # Limpiamos cada celda (quitamos guiones y ruidos)
                clean_row = [re.sub(r'[-_]{2,}', '', str(c)).strip() for c in row if c]
                
                # Solo procesamos si la fila tiene datos reales
                if len(clean_row) >= 2:
                    data["characteristics"].append({
                        "characteristic": clean_row[0],
                        "unit": clean_row[1] if len(clean_row) == 3 else "-",
                        "value": clean_row[-1]
                    })
    except: pass

    # B. LÓGICA DE LÍNEAS (REGEX) ---
    for i, line in enumerate(lines):
        if "Date" == line and i + 1 < len(lines):
            data["header_info"]["date"] = lines[i+1]

        # Material Reference 
        if "Material: Our / Your reference" in line and i + 3 < len(lines):
            material_lines = lines[i+1 : i+4]
            data["header_info"]["material_our_/_Your_reference"] = " ".join(material_lines)
        
        # Batch / Quantity / Railcar [cite: 23]
        if "Batch" in line and "Quantity" in line:
            batch = re.search(r'Batch\s+([A-Z0-9]+)', line)
            qty = re.search(r'Quantity\s+([\d,]+\s+LB)', line)
            railcar = re.search(r'Railcar\s+([A-Z0-9]+)', line)
            

            if batch: data["header_info"]["batch"] = batch.group(1)
            if qty: data["header_info"]["quantity"] = qty.group(1)
            if railcar: data["header_info"]["railcar"] = railcar.group(1)
            
    return data

def parse_bol(lines):
    data = {
        "header_info": {},
        "consignee": [],
        "send_freight_to": [],
        "carrier_instructions": {},
        "materials": []
    }
    
    header_captured = False
    full_text = "\n".join(lines)

    for i, line in enumerate(lines):
        # 1. ENCABEZADO PRINCIPAL (Regex de números de control)
        if not header_captured:
            h_match = re.search(r'(\d{10}-\d{3})\s+(\d{10})\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+(\d+)\s+([A-Z0-9]+)\s+(.*)', line)
            if h_match:
                data["header_info"].update({
                    "bill_of_lading_no": h_match.group(1),
                    "sales_order_no": h_match.group(2),
                    "shipping_date": h_match.group(3),
                    "ship_to_id": h_match.group(4),
                    "vehicle_id": h_match.group(5),
                    "customer_po": h_match.group(6)
                })
                header_captured = True

        # 2. ROUTE Y ORIGIN (Línea inmediatamente después del encabezado 'ROUTE')
        if "ROUTE" in line and "ORIGIN" in line:
            if i + 1 < len(lines):
                val_line = lines[i+1].strip()
                # Separamos por espacios grandes para obtener Route (izq) y Origin (der)
                parts = re.split(r'\s{3,}', val_line)
                if len(parts) >= 2:
                    data["header_info"]["route"] = parts[0]
                    data["header_info"]["origin"] = parts[-1]

        # 3. BLOQUE: INCO TERM / OFFEROR / SHIPPER
        if "INCO/FREIGHT TERM" in line:
            offer_parts = []
            ship_parts = []
            # Leemos las siguientes 4 líneas hasta llegar a 'SHIPPING CONDITION'
            for offset in range(1, 5):
                if i + offset < len(lines):
                    row = lines[i + offset]
                    if "SHIPPING CONDITION" in row or "1-800" in row: break
                    
                    cols = re.split(r'\s{2,}', row.strip())
                    
                    # El INCO TERM suele estar en la primera línea, primera columna
                    if offset == 1 and len(cols) > 0:
                        data["header_info"]["inco_term"] = cols[0]
                    
                    # Manejo dinámico de columnas para Direcciones
                    # Si hay 3 columnas: [INCO, OFFEROR, SHIPPER]
                    if len(cols) >= 3:
                        offer_parts.append(cols[1])
                        ship_parts.append(cols[2])
                    # Si hay 2 columnas: [OFFEROR, SHIPPER]
                    elif len(cols) == 2:
                        offer_parts.append(cols[0])
                        ship_parts.append(cols[1])
            
            data["header_info"]["offeror"] = ", ".join(offer_parts)
            data["header_info"]["shipper"] = ", ".join(ship_parts)

        # 4. SHIPPING CONDITION (Valor justo debajo del encabezado)
        if "SHIPPING CONDITION" in line:
            if i + 1 < len(lines):
                # Tomamos la primera parte de la línea de abajo
                cond_parts = re.split(r'\s{3,}', lines[i+1].strip())
                data["header_info"]["shipping_condition"] = cond_parts[0]

        # 5. CONSIGNEE Y SEND FREIGHT (Lectura de bloques paralelos)
        if "CONSIGNEE" in line and "SEND FREIGHT" in line:
            for j in range(1, 7):
                if i + j < len(lines):
                    row = lines[i + j].strip()
                    # Parar si detectamos el siguiente encabezado o fin de sección
                    if any(x in row for x in ["Carrier Instructions", "Section 7", "Pkgs"]): break
                    
                    # Split por espacios grandes o anclas de dirección (PO Box / HOUSTON)
                    parts = re.split(r'\s{3,}|(?=PO Box)|(?=HOUSTON)', row)
                    if len(parts) >= 1 and parts[0]:
                        data["consignee"].append(parts[0].strip())
                    if len(parts) >= 2 and parts[1]:
                        data["send_freight_to"].append(parts[1].strip())

        # 6. CARRIER INSTRUCTIONS (Fecha)
        if "Carrier Instructions" in line:
            d_match = re.search(r'(\d{4}-\d{2}-\d{2})', line)
            if d_match: data["carrier_instructions"]["delivery_date"] = d_match.group(1)

    # 7. EXTRACCIÓN GLOBAL (Embargos y Materiales)
    # Buscamos en todo el texto patrones técnicos fijos
    emb_match = re.search(r'EMBARGO NUMBER:\s*([A-Z0-9]+)', full_text)
    per_match = re.search(r'PERMIT NUMBER:\s*([A-Z0-9]+)', full_text)
    if emb_match: data["carrier_instructions"]["embargo_no"] = emb_match.group(1)
    if per_match: data["carrier_instructions"]["permit_no"] = per_match.group(1)

    # Pesos y Lotes (Patrón: Lote de 10 caracteres + Peso LBS)
    w_match = re.search(r'([A-Z0-9]{10})\s+([\d,]+\s+LBS)', full_text)
    if w_match:
        # Intentamos capturar sellos y otros pesos si existen
        s_match = re.search(r'Seal Numbers:\s*(\d+)', full_text)
        g_weight = re.search(r'Veh\.\sGross\sWeight:\s*([\d,]+)', full_text)
        
        data["materials"].append({
            "material": "POLYETHYLENE", # Se puede dinamizar buscando el texto previo al lote
            "lot_number": w_match.group(1),
            "net_weight": w_match.group(2),
            "seal_numbers": s_match.group(1) if s_match else "N/A",
            "gross_weight": g_weight.group(1) if g_weight else "N/A"
        })

    return data

def parse_coa(page, lines):
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
                # Si falló la asignación por columnas, limpiamos los guiones y forzamos el guardado
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

import re

def parse_nova(page):
   
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

        # PARTE 1: ENCABEZADOS PRINCIPALES 
        
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

            
        # PARTE 2: BLOQUES DE FECHAS Y ADUANAS 

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

        #  PARTE 3: COLUMNA IZQUIERDA Y SELLOS ---

        # 11. CONSIGNEE (Coord: X < 350, Y de 115 a 205)
        if x < 350 and 115 < y < 205:
            y_rounded = round(y, 1)
            if y_rounded not in cons_address_lines:
                cons_address_lines[y_rounded] = []
            cons_address_lines[y_rounded].append(txt)

        # 12. DESTINATION & ROUTE (Coord: X < 350, Y de 225 a 250)
        # Captura "SAN LUIS POTOSI, SL" y la ruta en un solo campo
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

    # PARTE 4: TABLA DE MATERIALES 
    
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


def parse_westlake(all_text):
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

    # 3. TABLA DE PROPIEDADES ( 
    
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
    """Identificación por prefijo exacto en el nombre del archivo"""
    
    name = filename.lower().strip()
    
    if name[:4] == "nova":
        return "nova"
        
    if name[:3] == "coa":
        return "coa"
        
    if name[:8] == "westlake":
        return "westlake"

    if name[:7] == "bayport":
        return "bayport"

    if name[:3] == "bol":
        return "bol"

    # RESPALDO: Si no cumple con el nombre, buscamos en el contenido como plan B
    full_text = "\n".join(lines).lower()
    if "westlake polymers" in full_text: return "westlake"
    if "nova chemicals" in full_text: return "nova"
    if "bayport polymers" in full_text: return "bayport"
    if "equistar" in full_text and "bill of lading" in full_text: return "bol"
    if "equistar" in full_text or "analysis" in full_text: return "coa"
        
    return "unknown"

#  SECCIÓN 3: ENDPOINT PRINCIPAL 

@app.post("/clean-pdf")
async def clean_pdf(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        # 1. INTENTO DE EXTRACCIÓN DIRECTA (pdfplumber)
        all_text = ""
        first_page = None
        
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            first_page = pdf.pages[0] 
            for p in pdf.pages:
                text = p.extract_text()
                if text: 
                    all_text += text + "\n"
        
        # 2. DETECCIÓN DE "PDF FANTASMA" -> ACTIVAR OCR
        method_used = "Direct"
        if not all_text.strip():
            method_used = "OCR"
            # Convertimos el PDF a imágenes para que Tesseract lo pueda leer
            images = convert_from_bytes(contents, poppler_path=POPPLER_PATH)
            for img in images:
                
                all_text += pytesseract.image_to_string(img, lang='spa+eng') + "\n"

        # 3. LIMPIEZA DE LÍNEAS (Estandarización)
        lines = [l.strip() for l in all_text.replace('\xa0', ' ').split('\n') if l.strip()]
        
        if not lines:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo")

        # 4. IDENTIFICACIÓN Y PARSEO
        parser_type = get_parser_type(file.filename, lines)
        
        if parser_type == "bayport":
            # Para Bayport seguimos usando las coordenadas de first_page
            structured_data = parse_bayport(first_page, lines)
        elif parser_type == "bol_equistar" or parser_type == "bol":
            # Para el BOL usamos la nueva lógica basada en el texto de OCR 
            structured_data = parse_bol(lines)
        elif parser_type == "coa":
            structured_data = parse_coa(first_page, lines)
        elif parser_type == "nova":
            structured_data = parse_nova(first_page)
        elif parser_type == "westlake":
            structured_data = parse_westlake(all_text)

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)