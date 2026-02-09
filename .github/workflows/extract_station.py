from datetime import datetime
import os
import requests
from bs4 import BeautifulSoup
import re
import time
import json
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from pathlib import Path
from datetime import datetime, timezone
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

print("UTC time:", datetime.now(timezone.utc))
print("Local time:", datetime.now())
print("System TZ name:", time.tzname)

service = Service(ChromeDriverManager().install())
options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")
options.add_argument(
    "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari/537.36"
)
BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "data" / "extract"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

current_date = datetime.now().strftime("%Y-%m-%d")
run_tag = datetime.now().strftime("%H%M")
file_name = f"results_{current_date}_{run_tag}.txt"
full_path = OUTPUT_DIR / file_name

with open(full_path, "a", encoding="utf-8") as f:
    f.write(f"\nWeather data extracted at {datetime.now().strftime('%H:%M:%S')}\n")

print(f"File saved to: {full_path}")

print(f"File saved to: {full_path}")
# --- Configuration ---
STATION_MAP = {
    "IKERKIRA2": "Arillas", "IAVLIO1": "Avliotes", "IPERIT4": "Acharavi",
    "IKAROU2": "Gialos Karousadon", "ILOUTS1": "Loutses1",
    "ILOUTS2": "Loutses Anapaftiria", "IKASSI2": "Kassiopi", "ISINIE3": "Sinies Porta",
    "INISSA6": "Old Sinies", "IYANNA5": "Ropa", "ICORFU22": "Laiki Agora",
    "ICORFU20": "Kentro Kofineta", "ICORFU9": "1st Epal", "ICORFU24": "Garitsa",
    "IKOBIT2": "Kobitsi", "IGASTO3": "Perama", "IKALAF4": "Kothoniki", "ICORFU8": "Agios Georgios Argyr",
    "ICHLOM1": "Chlomos", "IARGYR6": "Perivoli", "ISAYAD1": "Sagiada",
    "IIGOUM1": "Igoumenitsa", "IU0389U02": "Filothei Thesprot"
}

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def get_weather_data(station_id):
    name_station = STATION_MAP.get(station_id, "Unknown")
    url = f"https://www.wunderground.com/dashboard/pws/{station_id}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        tables = soup.find_all(class_='summary-table')
        
        if len(tables) < 2:
            return {"ID": station_id, "Name": name_station, "Status": "Offline"}

        # --- Table 1: Temps & Rain ---
        table1 = tables[0]
        temp_containers = table1.find_all('span', class_='wu-unit-temperature')
        floats = re.findall(r'\d+\.\d+', table1.get_text())
        values = [float(x) for x in floats]

        # FIX: Ensure we always return a dict, even if data is missing
        if len(values) < 4:
            return {"ID": station_id, "Name": name_station, "Status": "Incomplete Data"}

        unit_label = temp_containers[0].find('span', class_='wu-label').text.strip() if temp_containers else "F"
        raw_temps = values[:3] 
        
        if "F" in unit_label:
            h, l, a = [(t - 32) * 5/9 for t in raw_temps]
            rain = values[-1] * 25.4
        else:
            h, l, a = raw_temps
            rain = values[-1]

        # --- Table 2: Wind ---
        wind_gust_kmh = 0.0
        for row in tables[1].find_all('tr'):
            if "Wind Gust" in row.get_text():
                v = row.find('span', class_='wu-value')
                l_unit = row.find('span', class_='wu-label')
                if v:
                    raw_text = v.text.strip().replace(',', '')
                    if raw_text:
                        raw_gust = float(raw_text)
                        wind_gust_kmh = raw_gust * 1.60934 if l_unit and "mph" in l_unit.text else raw_gust
                break

        return {
            "ID": station_id, "Name": name_station, "Status": "OK",
            "High": h, "Low": l, "Rain": rain, "Gust": wind_gust_kmh
        }

    except Exception as e:
        return {"ID": station_id, "Name": name_station, "Status": "Error"}

if __name__ == "__main__":
    all_results = []
    with open(full_path, "a", encoding="utf-8") as f:
        f.write(f"Scraping stations, please wait...")

    for station in STATION_MAP.keys():
        print(f" > Fetching {station}...")
        data = get_weather_data(station)
        all_results.append(data)
        time.sleep(1)

    # --- FINAL SUMMARY TABLE ---
    with open(full_path, "a", encoding="utf-8") as f:
        f.write("\n" + "="*85 + "\n")
        f.write(f"{'STATION NAME':<20} | {'ID':<10} | {'HIGH':<7} | {'LOW':<7} | {'RAIN':<7} | {'GUST':<7}")
        f.write("-" * 85)
        f.write("\n")
        
        for res in all_results:
            # Check if res is not None before calling .get()
            if res and res.get("Status") == "OK":
                line =f"{res['Name']:<20} | {res['ID']:<10} | {res['High']:>5.1f} °C | {res['Low']:>5.1f} °C | {res['Rain']:>5.1f} mm | {res['Gust']:>5.1f} km/h"
            else:
                status_msg = res.get("Status") if res else "Unknown Error"
                line = f"{res['Name'] if res else 'Unknown':<20} | {res['ID'] if res else '---':<10} | {status_msg:^35}"
            f.write(line)
            f.write("\n")
            print(line.strip())      
        f.write("="*85)
    print(f"\nSuccess! Results saved to: {full_path}")


url = "https://valanio-kerkyra.meteoclub.gr/"

def get_valaneio_data():
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # --- 1. Extract High & Low Temperatures ---
        temp_td = soup.find('td', align="left", bgcolor="#CCFFFF")
        high_temp, low_temp = "N/A", "N/A"
        if temp_td:
            paragraphs = temp_td.find_all('p')
            if len(paragraphs) >= 2:
                h_m = re.search(r'(\d+\.\d+)°C', paragraphs[0].get_text())
                l_m = re.search(r'(\d+\.\d+)°C', paragraphs[1].get_text())
                high_temp = h_m.group(1) if h_m else "N/A"
                low_temp = l_m.group(1) if l_m else "N/A"

        # --- Helper Function for Table Labels ---
        def get_value_by_label(label_text, unit_regex):
            # Find the cell that contains the label
            label_td = None
            for td in soup.find_all('td'):
                if label_text in td.get_text():
                    label_td = td
                    break
            
            if label_td:
                # Get the next cell in the row
                value_td = label_td.find_next_sibling('td')
                if value_td:
                    # Search for the numeric pattern within that cell
                    text = value_td.get_text(strip=True)
                    match = re.search(unit_regex, text)
                    return match.group(1) if match else "N/A"
            return "N/A"

        # --- 2. Extract Today's Rain ---
        rain_val = get_value_by_label("Today's Rain", r'(\d+\.\d+)\s*mm')

        # --- 3. Extract High Wind Speed ---
        # Note: The regex handles 'km/hr' and 'km/h'
        wind_val = get_value_by_label("High Wind Speed", r'(\d+\.\d+)\s*km/hr')

        # --- Print Results ---
        with open(full_path, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write(f"\n--- Weather Data for Valaneio ---")
            f.write(f"\nHigh Temp: {high_temp}°C | Low Temp: {low_temp}°C | Rain Today: {rain_val} mm | Max Wind Gust: {wind_val} km/h")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    get_valaneio_data()


# --- Configuration ---
URL_1 = "https://penteli.meteo.gr/stations/kerkyra/"
URL_2 = "https://penteli.meteo.gr/stations/paxoi/"
URL_3 = "https://penteli.meteo.gr/stations/petalia/"
URL_4 = "https://penteli.meteo.gr/stations/acharavi/"
URL_5 = "https://penteli.meteo.gr/stations/igoumenitsa/"
TIMEOUT_SECONDS = 15 

options = webdriver.ChromeOptions()
options.add_argument("--headless")
options.add_argument("--disable-gpu")
options.add_argument("--no-sandbox")
options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def scrape_weather_selenium(url):
    driver = None
    results = {"high_temp": "N/A", "low_temp": "N/A", "rain": "N/A", "gust": "N/A"}
    
    try:
        driver = webdriver.Chrome(options=options)
        driver.get(url)
        wait = WebDriverWait(driver, TIMEOUT_SECONDS)

        # --- 1. Extract Temperatures (High & Low) ---
        try:
            temp_xpath = "//span[contains(text(), 'High Temperature')]/parent::div/following-sibling::div"
            temp_container = wait.until(EC.presence_of_element_located((By.XPATH, temp_xpath)))
            spans = temp_container.find_elements(By.TAG_NAME, "span")
            if len(spans) >= 2:
                # Flexible regex for whole or decimal numbers
                h_m = re.search(r'(\d+\.?\d*\s*°C)', spans[0].text)
                l_m = re.search(r'(\d+\.?\d*\s*°C)', spans[1].text)
                if h_m: results["high_temp"] = h_m.group(1)
                if l_m: results["low_temp"] = l_m.group(1)
        except Exception: pass

        # --- 2. Extract Today's Rain ---
        try:
            rain_xpath = "//span[contains(text(), \"Today's Rain\")]/parent::div/following-sibling::div/span"
            rain_element = wait.until(EC.presence_of_element_located((By.XPATH, rain_xpath)))
            rain_match = re.search(r'(\d+\.?\d*\s*mm)', rain_element.text)
            if rain_match:
                results["rain"] = rain_match.group(1)
        except Exception: pass

        # --- 3. Extract High Wind Gust ---
        try:
            # Finding the row labeled 'High Wind Gust'
            gust_xpath = "//span[contains(text(), 'High Wind Gust')]/parent::div/following-sibling::div/span"
            gust_element = wait.until(EC.presence_of_element_located((By.XPATH, gust_xpath)))
            # Regex handles 'Km/h', 'km/h', or 'km/hr' regardless of case
            gust_match = re.search(r'(\d+\.?\d*)\s*Km/h', gust_element.text, re.IGNORECASE)
            if gust_match:
                results["gust"] = f"{gust_match.group(1)} km/h"
        except Exception: pass

        return results

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return results
    finally:
        if driver:
            driver.quit()

# --- Scrape both URLs ---
#print("--- Starting Scraper ---")
data1 = scrape_weather_selenium(URL_1)
data2 = scrape_weather_selenium(URL_2)
data3 = scrape_weather_selenium(URL_3)
data4 = scrape_weather_selenium(URL_4)
data5 = scrape_weather_selenium(URL_5)
with open(full_path, "a", encoding="utf-8") as f:
    f.write("\n")
    f.write(f"\n--- Summary ---\n")
    # Cleaner formatted output template
    # Added \n at the end of the template string so every line breaks automatically
    template = "{:<18} | High: {:>8} | Low: {:>8} | Rain: {:>8} | Gust: {:>9}\n"
    
    # 1. Write the Header
    f.write(template.format("Station", "Temp", "Temp", "Today", "Max"))
    f.write("-" * 80 + "\n")
    
    # 2. Write the Data Rows
    f.write(template.format("Gouvia", data1['high_temp'], data1['low_temp'], data1['rain'], data1['gust']))
    f.write(template.format("Paxoi", data2['high_temp'], data2['low_temp'], data2['rain'], data2['gust']))
    f.write(template.format("Petaleia", data3['high_temp'], data3['low_temp'], data3['rain'], data3['gust']))
    f.write(template.format("AcharaviEAA", data4['high_temp'], data4['low_temp'], data4['rain'], data4['gust']))
    f.write(template.format("MavroudiThesprot", data5['high_temp'], data5['low_temp'], data5['rain'], data5['gust']))

print(f"Results successfully appended to {full_path}")

url = "https://www.meteociel.fr/temps-reel/obs_villes.php?code2=16641"
headers = {'User-Agent': 'Mozilla/5.0'}

try:
    response = requests.get(url, headers=headers)
    response.encoding = response.apparent_encoding 
    soup = BeautifulSoup(response.text, 'html.parser')

    # 1. Find the yellow summary table specifically
    summary_table = soup.find('table', bgcolor="#FFFF99")

    results = {
        "Max Temp": "N/A",
        "Min Temp": "N/A",
        "Max Gust": "N/A",
        "Rain": "N/A"
    }

    if summary_table:
        rows = summary_table.find_all('tr')
        if len(rows) >= 2:
            # The first row contains headers (labels)
            headers_cells = rows[0].find_all('td')
            # The second row contains the actual data
            data_cells = rows[1].find_all('td')

            for i, cell in enumerate(headers_cells):
                label = cell.get_text(strip=True)
                
                # Check column index i and grab corresponding data from data_cells[i]
                if "Température Maxi" in label:
                    results["Max Temp"] = data_cells[i].get_text(strip=True)
                elif "Température Mini" in label:
                    results["Min Temp"] = data_cells[i].get_text(strip=True)
                elif "Rafale maxi" in label:
                    results["Max Gust"] = data_cells[i].get_text(strip=True)
                elif "Précipitations" in label:
                    results["Rain"] = data_cells[i].get_text(strip=True)
    with open(full_path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.write("\n--- Weather Data for Aerodromio ---\n")
        f.write(f"Max Temp: {results['Max Temp']} | Min Temp: {results['Min Temp']} | Rain Today: {results['Rain']} | Max Gust: {results['Max Gust']}")

except Exception as e:
    print(f"Error: {e}")


# --- Configuration ---
# Dictionary mapping Station ID to Friendly Name
STATION_NAMES = {
    "d7463552240": "AgiosPanteleimonas",
    "d0774314531": "Agni",
    "d7746504386": "Nissaki",
    "d7550631437": "Ypsos",
    "d6245085291": "Kothoniki",
    "d1871029033": "Perama",
    "d1594180981": "Gastouri",
    "d2603547554": "Milia Kynopiaston",
    "d5203070705": "Agioi Deka",
    "d4591805891": "Petriti", 
    "d3332581754": "GraikoxoriThesprot", 
    "d0228718460": "FiliatesThesprot"
}

# Generate URL list automatically from the IDs above
URLS = [f"https://app.weathercloud.net/{s_id}#current" for s_id in STATION_NAMES.keys()]

TIMEOUT_SECONDS = 15
options = webdriver.ChromeOptions()
options.add_argument("--headless") 
options.add_argument("--window-size=1920,1080")
options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def scrape_station(driver, url):
    # Extract ID from URL to find the name
    station_id = url.split('/')[-1].replace('#current', '')
    name = STATION_NAMES.get(station_id, station_id) # Fallback to ID if name not found
    
    print(f"Targeting: {name} ({station_id})...")
    driver.get(url)
    wait = WebDriverWait(driver, TIMEOUT_SECONDS)
    
    # --- 1. HANDLE CONSENT ---
    try:
        consent_xpath = "//button[contains(., 'onsent')] | //button[contains(., 'gree')] | //button[contains(., 'ccept')]"
        consent_button = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, consent_xpath)))
        consent_button.click()
        time.sleep(1)
    except:
        pass 

    # --- 2. SCRAPE DATA ---
    gauge_ids = {
        "Max Temp": "gauge-temp-max-day",
        "Min Temp": "gauge-temp-min-day",
        "Daily Rain": "gauge-rain-min-day" # Changed to standard rain-day ID
    }
    
    results = {"Name": name}

    for label, element_id in gauge_ids.items():
        try:
            element = wait.until(EC.presence_of_element_located((By.ID, element_id)))
            wait.until(lambda d: re.search(r'\d', element.text))
            
            raw_text = element.text.strip()
            match = re.search(r'(-?\d+[.,]?\d*)', raw_text)
            results[label] = match.group(1) if match else "0.0"
        except:
            results[label] = "N/A"
            
    return results

# --- Main Execution ---
driver = None
all_station_data = []

try:
    driver = webdriver.Chrome(options=options)
    
    for target_url in URLS:
        try:
            data = scrape_station(driver, target_url)
            all_station_data.append(data)
        except Exception as e:
            print(f"Failed to scrape: {e}")

    # --- FINAL SUMMARY TABLE ---
    with open(full_path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.write("\n" + "="*65 + "\n")
        f.write(f"\n{'LOCATION':<20} | {'MAX':<8} | {'MIN':<8} | {'RAIN':<8}\n")
        f.write("-" * 65)
        for row in all_station_data:
            f.write(f"\n{row['Name']:<20} | {row['Max Temp']:<8} | {row['Min Temp']:<8} | {row['Daily Rain']:<8}\n")
        f.write("="*65)
        f.write("\n")

except Exception as e:
    print(f"Driver Error: {e}")
finally:
    if driver:
        driver.quit()


URL = "https://ionianweather.gr/stations/stas.html"
TARGET_CODES = {"CRF-1", "CRF-2", "CRF-3", "CRF-4", "PAX-1"}

rows = []

try:
    # 1. Attempt to download data with a timeout (crucial for automation)
    response = requests.get(URL, timeout=15) 
    response.raise_for_status()
    
    # 2. Parse JSON
    data = response.json() 
    stations = data.get("stats", {})

    if not stations:
        raise ValueError("The 'stats' key is missing or empty in the JSON data.")

    # 3. Process the stations
    for station_name, info in stations.items():
        if info.get("code") in TARGET_CODES:
            rows.append({
                "station": station_name,
                "code": info.get("code"),
                "Max Temp": info.get("Max Temperature"),
                "Min Temp": info.get("Min Temperature"),
                "Rain": info.get("Rain By Day"),
                "Gust": info.get("Gust KlmPerHour"),
            })

except requests.exceptions.RequestException as e:
    error_msg = f"NETWORK ERROR: Could not reach {URL}. Details: {e}"
    print(error_msg)
    with open(full_path, "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now()}] {error_msg}\n")
    rows = [] # Ensure script continues with an empty list

except Exception as e:
    error_msg = f"SCRIPT ERROR: An unexpected error occurred: {e}"
    print(error_msg)
    with open(full_path, "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now()}] {error_msg}\n")
    rows = []

# --- Save Results if we have any ---
if rows:
    df = pd.DataFrame(rows)
    with open(full_path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.write(f"\n--- Extract Success: {datetime.now().strftime('%H:%M:%S')} ---\n")
        # Converting dataframe to string for the text file
        f.write(df.to_string(index=False))
        f.write("\n" + "="*50)
    print("Data saved successfully.")
else:
    print("No data was collected.")
