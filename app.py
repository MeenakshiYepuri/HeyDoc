from flask import Flask, render_template_string, request, redirect, session, flash, jsonify, send_from_directory
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import random
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

app = Flask(__name__)
app.secret_key = "your_secret_key_please_change_this_for_security" # **IMPORTANT: Change this to a strong, random key in production!**

# Serve local logo/bot icon image
@app.route("/file.jpeg")
def serve_local_logo():
    try:
        base_dir = os.path.dirname(__file__)
        return send_from_directory(base_dir, "file.jpeg")
    except Exception as _e:
        # Fallback: return 404 if file not found
        return ("", 404)

# SMTP Configuration for email notifications
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_SENDER = os.getenv("MAIL_USERNAME", "Hey Dochomoeoclinic9@gmail.com")
EMAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "xlut dnhu ymvh qntw")
DOCTOR_EMAIL = "eedevnsskjayanth@gmail.com"

client = MongoClient("mongodb+srv://varma:varma1225@varma.f5zdh.mongodb.net/?retryWrites=true&w=majority&appName=varma")
db = client["hospital_bot"]
doctors_collection = db["doctor"]
appointments_collection = db["appointments"]
prescriptions_collection = db["prescriptions"]  # New collection for prescriptions
blocked_slots_collection = db["blocked_slots"]  # Stores doctor-held/blocked slots
loc_aval_collection = db["LocAval"]  # Stores availability created from UI
branches_collection = db["branch"]  # Stores clinic branches

# --- Location/Timings configuration ---
# Supported clinic locations that determine which Mongo collection to read working hours from
AVAILABLE_CITIES = ["Akola", "Hyderabad", "Pune"]  # still used elsewhere; form will accept free-text

# Map each city to its timings collection (as shown in MongoDB Atlas screenshot)
# If these collections do not exist in your cluster, the code will gracefully fall back to defaults
CITY_TO_TIMINGS_COLLECTION = {
    "Akola": db.get_collection("Akola_Doctor-overiden_hospital_timings"),
    "Hyderabad": db.get_collection("Hyderabad_Doctor-overiden_hospital_timings"),
    "Pune": db.get_collection("Pune_Doctor-overiden_hospital_timings"),
}

# --- Email notification function ---
def send_cancellation_email(patient_name, patient_email, appointment_date, appointment_time):
    """Send cancellation email to patient"""
    try:
        if not patient_email or patient_email == "No email provided":
            return False
            
        message_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <p>Dear {patient_name},</p>
            <p>Your appointment scheduled for <strong>{appointment_date}</strong> at <strong>{appointment_time}</strong> has been cancelled.</p>
            <p>If you have any questions or would like to reschedule, please contact us.</p>
            <p>Best Regards,</p>
            <p><strong>Hey Doc!</strong></p>
        </body>
        </html>
        """

        msg = MIMEMultipart()
        msg["From"] = EMAIL_SENDER
        msg["To"] = patient_email
        msg["Subject"] = "❌ Appointment Cancelled"
        msg.attach(MIMEText(message_html, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, patient_email, msg.as_string())
        server.quit()
        
        return True
    except Exception as e:
        print(f"Error sending cancellation email: {e}")
        return False

# --- Helper: parse times like "11:00 AM" → datetime.time ---
def _parse_12h_to_time(value: str):
    try:
        return datetime.strptime(value.strip(), "%I:%M %p").time()
    except Exception:
        # Try already-24h formats like "18:00"
        try:
            return datetime.strptime(value.strip(), "%H:%M").time()
        except Exception:
            return None


# --- Helper: get working hour ranges for a city/date from Mongo ---
def _get_time_ranges_for_city(city, for_date=None):
    """Return list of (start_time, end_time) for the city and optional date.
    Priority order:
      1) LocAval date-specific override for city
      2) LocAval Default:true for city
      3) Legacy city collections mapping
    Note: Only morning and evening shifts are used (afternoon ignored).
    """
    ranges = []
    try:
        # 1) Try LocAval collection first
        doc = None
        if for_date:
            try:
                dt_obj = datetime.strptime(for_date, "%d-%m-%Y")
                ddmmyyyy = dt_obj.strftime("%d-%m-%Y")
                doc = loc_aval_collection.find_one({"location": city, "date": ddmmyyyy})
                if not doc:
                    doc = loc_aval_collection.find_one({"location": city, "date": for_date})
            except Exception:
                pass
        if doc is None:
            doc = loc_aval_collection.find_one({"location": city, "Default": {"$in": [True, "true", "True"]}})

        if not doc:
            # 2) Fallback to legacy per-city collections
            col = CITY_TO_TIMINGS_COLLECTION.get(city)
            if col is not None:
                if for_date:
                    try:
                        dt_obj = datetime.strptime(for_date, "%d-%m-%Y")
                        ddmmyyyy = dt_obj.strftime("%d-%m-%Y")
                        doc = col.find_one({"date": ddmmyyyy}) or col.find_one({"date": for_date})
                    except Exception:
                        pass
                if doc is None:
                    doc = col.find_one({"Default": {"$in": [True, "true", "True"]}}) or col.find_one({})

        if doc and isinstance(doc.get("working_hours"), dict):
            wh = doc["working_hours"]
            for key in ["morning_shift", "evening_shift"]:  # afternoon intentionally ignored
                shift = wh.get(key)
                if isinstance(shift, dict):
                    start_label = shift.get("start")
                    end_label = shift.get("end")
                    start_time = _parse_12h_to_time(start_label) if start_label else None
                    end_time = _parse_12h_to_time(end_label) if end_label else None
                    if start_time and end_time:
                        ranges.append((start_time, end_time))

        # Defaults if nothing configured
        if not ranges:
            ranges = [
                (datetime.strptime("07:00", "%H:%M").time(), datetime.strptime("12:00", "%H:%M").time()),
                (datetime.strptime("18:00", "%H:%M").time(), datetime.strptime("21:00", "%H:%M").time()),
            ]
    except Exception:
        ranges = [
            (datetime.strptime("07:00", "%H:%M").time(), datetime.strptime("12:00", "%H:%M").time()),
            (datetime.strptime("18:00", "%H:%M").time(), datetime.strptime("21:00", "%H:%M").time()),
        ]
    return ranges


# --- Helper: normalize and validate Indian phone numbers (10 digits) ---
def normalize_indian_phone(raw_phone: str):
    """
    Accepts inputs like '+91XXXXXXXXXX', '91XXXXXXXXXX', '0XXXXXXXXXX', or just 10 digits.
    Returns ('+91XXXXXXXXXX', None) if valid, otherwise (None, error_message).
    Only accepts exactly 10 digits after removing leading 0, +91, or 91.
    Shows error if more or less than 10 digits are entered.
    """
    try:
        if raw_phone is None:
            return None, "Phone number is required."
        digits_only = ''.join(ch for ch in str(raw_phone) if ch.isdigit())
        # Remove leading 0, 91, or +91 if present
        if digits_only.startswith('0'):
            digits_only = digits_only[1:]
        elif digits_only.startswith('91') and len(digits_only) > 10:
            digits_only = digits_only[2:]
        # After removing prefix, must be exactly 10 digits
        if len(digits_only) != 10:
            return None, "Enter a valid 10-digit phone number (do not enter more or less than 10 digits)."
        return f"+91{digits_only}", None
    except Exception:
        return None, "Enter a valid 10-digit phone number."

# ...existing code...

# --- Helper function to generate time slots (optionally city-aware) ---
# --- Helper function to generate time slots (optionally city-aware) ---
def generate_time_slots(city: str = None, for_date: str = None):
    """Generate 10-minute slots from the city's working hours (optionally for a specific date).
    If city is None or timings not found, defaults are used.
    Returns slots in 12-hour format with AM/PM.
    Filters out past time slots if the date is today."""
    slots = []
    ranges = _get_time_ranges_for_city(city, for_date) if city else _get_time_ranges_for_city("Hyderabad", for_date)

    # Get current time for filtering past slots
    now = datetime.now()
    current_time_str = now.strftime("%H:%M")
    
    # Check if the date is today
    is_today = False
    if for_date:
        try:
            # Try to parse the date and compare with today
            if len(for_date) == 10 and for_date[4] == '-' and for_date[7] == '-':
                # YYYY-MM-DD format
                date_obj = datetime.strptime(for_date, "%Y-%m-%d")
            elif len(for_date) == 10 and for_date[2] == '-' and for_date[5] == '-':
                # DD-MM-YYYY format
                date_obj = datetime.strptime(for_date, "%d-%m-%Y")
            else:
                date_obj = None
                
            if date_obj and date_obj.date() == now.date():
                is_today = True
        except ValueError:
            # If date parsing fails, assume it's not today
            pass

    for start_time, end_time in ranges:
        start_dt = datetime.combine(datetime.today(), start_time)
        end_dt = datetime.combine(datetime.today(), end_time)
        current_time = start_dt
        while current_time < end_dt:
            slot_time_str = current_time.strftime("%I:%M %p")
            slot_time_24 = current_time.strftime("%H:%M")
            
            # If it's today, only include future or current time slots
            if is_today:
                if slot_time_24 >= current_time_str:
                    slots.append(slot_time_str)
            else:
                # For future dates, include all slots
                slots.append(slot_time_str)
            
            current_time += timedelta(minutes=10)
    return slots

# ...existing code...

# --- Helper function to clean up appointments with missing fields ---
def cleanup_appointments():
    """Clean up appointments that might have missing or incorrect field names"""
    try:
        # Find appointments that might have different field names
        appointments_to_update = []
        
        # Check for appointments with 'patient_name' instead of 'name'
        appointments_with_patient_name = appointments_collection.find({"patient_name": {"$exists": True}})
        for appointment in appointments_with_patient_name:
            if 'name' not in appointment:
                appointments_to_update.append({
                    "_id": appointment["_id"],
                    "name": appointment.get("patient_name", "Unknown Patient")
                })
        
        # Check for appointments with 'patient_phone' instead of 'phone'
        appointments_with_patient_phone = appointments_collection.find({"patient_phone": {"$exists": True}})
        for appointment in appointments_with_patient_phone:
            if 'phone' not in appointment:
                appointments_to_update.append({
                    "_id": appointment["_id"],
                    "phone": appointment.get("patient_phone", "No phone")
                })
        
        # Update appointments with missing fields
        for update_data in appointments_to_update:
            appointment_id = update_data.pop("_id")
            appointments_collection.update_one(
                {"_id": appointment_id},
                {"$set": update_data}
            )
        
        # Also ensure all appointments have required fields
        all_appointments = appointments_collection.find({})
        for appointment in all_appointments:
            updates_needed = {}
            
            # Ensure appointment_id exists
            if 'appointment_id' not in appointment:
                date_str = datetime.now().strftime("%Y%m%d")
                random_num = str(random.randint(1, 9999)).zfill(4)
                updates_needed['appointment_id'] = f"HeyDoc-{date_str}-{random_num}"
            
            # Ensure name exists
            if 'name' not in appointment or not appointment['name']:
                updates_needed['name'] = 'Unknown Patient'
            
            # Ensure phone exists
            if 'phone' not in appointment or not appointment['phone']:
                updates_needed['phone'] = 'No phone'
            
            # Ensure email exists
            if 'email' not in appointment or not appointment['email']:
                updates_needed['email'] = 'No email provided'
            
            # Ensure address exists
            if 'address' not in appointment or not appointment['address']:
                updates_needed['address'] = 'No address provided'
            
            # Ensure symptoms exists
            if 'symptoms' not in appointment or not appointment['symptoms']:
                updates_needed['symptoms'] = 'No symptoms provided'
            
            # Ensure date exists
            if 'date' not in appointment or not appointment['date']:
                updates_needed['date'] = datetime.now().strftime("%Y-%m-%d")
            
            # Ensure time exists
            if 'time' not in appointment or not appointment['time']:
                updates_needed['time'] = '09:00'
            
            # Ensure status exists
            if 'status' not in appointment or not appointment['status']:
                updates_needed['status'] = 'pending'
            
            # Apply updates if needed
            if updates_needed:
                appointments_collection.update_one(
                    {"_id": appointment["_id"]},
                    {"$set": updates_needed}
                )
                print(f"Updated appointment {appointment.get('appointment_id', 'NO_ID')} with missing fields: {list(updates_needed.keys())}")
        
    except Exception as e:
        print(f"Error cleaning up appointments: {e}")

# --- Helper function to get booked time slots for a specific date (and optional city) ---
def get_booked_slots_for_date(date, city=None, exclude_appointment_id=None):
    """Get list of booked time slots for a specific date, optionally filtered by city."""
    # Normalize incoming date to support both YYYY-MM-DD (from <input type="date">)
    # and DD-MM-YYYY (stored in Mongo)
    date_candidates = [date]
    try:
        if len(date) == 10 and date[4] == '-' and date[7] == '-':
            # Looks like YYYY-MM-DD → add DD-MM-YYYY variant
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_candidates.append(dt.strftime("%d-%m-%Y"))
        elif len(date) == 10 and date[2] == '-' and date[5] == '-':
            # Looks like DD-MM-YYYY → add YYYY-MM-DD variant
            dt = datetime.strptime(date, "%d-%m-%Y")
            date_candidates.append(dt.strftime("%Y-%m-%d"))
    except Exception:
        pass

    query = {"date": {"$in": date_candidates}}
    if city:
        query["location"] = city
    if exclude_appointment_id:
        query["appointment_id"] = {"$ne": exclude_appointment_id}
    
    # Exclude past times if the date is today
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    cutoff_time = now.strftime("%H:%M") if date == today_str else None

    def not_past(time_str: str) -> bool:
        if cutoff_time is None:
            return True
        return time_str >= cutoff_time

    booked_appointments = appointments_collection.find(query)
    booked_slots = [appointment["time"] for appointment in booked_appointments if not_past(appointment["time"])]
    
    # Include blocked slots for the date (optionally by city)
    blocked_query = {"date": {"$in": date_candidates}}
    if city:
        blocked_query["location"] = city
    blocked = blocked_slots_collection.find(blocked_query)
    blocked_times = [b.get("time") for b in blocked if not_past(b.get("time"))]
    
    # Merge and deduplicate
    all_unavailable = sorted(list({*booked_slots, *blocked_times}))
    return all_unavailable

# --- Existing Templates (included for completeness) ---
home_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hey Doc!</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
    <link rel="stylesheet" href="https://www.gstatic.com/dialogflow-console/fast/df-messenger/prod/v1/themes/df-messenger-default.css">
    <script src="https://www.gstatic.com/dialogflow-console/fast/df-messenger/prod/v1/df-messenger.js"></script>
</head>
<body class="font-sans bg-white">
    <nav class="bg-white shadow-lg fixed w-full top-0 z-50">
        <div class="max-w-6xl mx-auto px-4">
            <div class="flex justify-between items-center py-4">
                <div class="flex items-center space-x-3">
                    <img src="/file.jpeg" alt="Hey Doc Logo" class="h-12 w-12 rounded-full object-cover">
                    <span class="text-xl font-bold text-gray-800">Hey Doc!</span>
                </div>
                <div class="hidden md:flex space-x-8 text-gray-700 font-medium" id="navbar-menu-desktop">
                    <a href="#home" class="hover:text-teal-600 transition-colors">Home</a>
                    <a href="#doctor" class="hover:text-teal-600 transition-colors">Meet Doctor</a>
                    <a href="#contact" class="hover:text-teal-600 transition-colors">Contact</a>
                    <a href="/login" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">Doctor Login</a>
                </div>
                <div class="md:hidden">
                    <button id="mobile-menu-button" class="text-gray-700">
                        <i class="ri-menu-line text-2xl"></i>
                    </button>
                </div>
            </div>
        </div>
        <div id="mobile-menu" class="md:hidden hidden bg-white py-2 shadow-lg">
            <a href="#home" class="block px-4 py-2 text-gray-700 hover:bg-gray-100">Home</a>
            <a href="#doctor" class="block px-4 py-2 text-gray-700 hover:bg-gray-100">Meet Doctor</a>
            <a href="#contact" class="block px-4 py-2 text-gray-700 hover:bg-gray-100">Contact</a>
            <a href="/login" class="block px-4 py-2 text-gray-700 hover:bg-gray-100">Doctor Login</a>
        </div>
    </nav>

    <section id="home" class="pt-20 min-h-screen bg-gradient-to-br from-teal-600 to-teal-300 text-white flex items-center">
        <div class="max-w-6xl mx-auto px-4 text-center">
            <h1 class="text-4xl md:text-6xl font-bold mb-6">
                Welcome to Hey Doc 
            </h1>
            <p class="text-xl mb-8 max-w-3xl mx-auto">
                Experience holistic homeopathic treatment tailored to your unique needs, guided by expertise and empathy. Our approach combines traditional healing wisdom with modern understanding to restore your natural balance.
            </p>
            <div class="flex flex-col md:flex-row items-center justify-center space-y-4 md:space-y-0 md:space-x-4">
                <a href="#doctor" class="w-full md:w-auto bg-white text-teal-600 px-8 py-3 rounded-lg font-semibold hover:bg-gray-100 transition-colors inline-block">
                    Meet the Doctor
                </a>
                <a href="#contact" class="w-full md:w-auto border-2 border-white text-white px-8 py-3 rounded-lg font-semibold hover:bg-white hover:text-teal-600 transition-colors inline-block">
                    Contact Us
                </a>
            </div>
        </div>
    </section>
    <section id="doctor" class="py-20 bg-white">
        <div class="max-w-6xl mx-auto px-4">
            <div class="bg-white rounded-xl shadow-lg p-8 max-w-4xl mx-auto">
                <div class="text-center mb-8">
                    <h2 class="text-3xl font-bold text-gray-800 mb-4">Dr. Priya Sharma</h2>
                    <p class="text-lg text-gray-600">BHMS, MD (Homeopathy), 15+ Years Experience</p>
                </div>
                
                <div class="grid md:grid-cols-3 gap-6 mb-8">
                    <div class="text-center">
                        <div class="w-16 h-16 bg-teal-100 rounded-full flex items-center justify-center mx-auto mb-3">
                            <i class="ri-mental-health-line text-teal-600 text-2xl"></i>
                        </div>
                        <h3 class="font-semibold text-gray-800">Psychiatry & Mental Health</h3>
                    </div>
                    <div class="text-center">
                        <div class="w-16 h-16 bg-teal-100 rounded-full flex items-center justify-center mx-auto mb-3">
                            <i class="ri-graduation-cap-line text-teal-600 text-2xl"></i>
                        </div>
                        <h3 class="font-semibold text-gray-800">Learning Disabilities</h3>
                    </div>
                    <div class="text-center">
                        <div class="w-16 h-16 bg-teal-100 rounded-full flex items-center justify-center mx-auto mb-3">
                            <i class="ri-heart-line text-teal-600 text-2xl"></i>
                        </div>
                        <h3 class="font-semibold text-gray-800">Mood Disorders</h3>
                    </div>
                </div>
                
                <div class="grid md:grid-cols-3 gap-6">
                    <div class="flex items-center gap-3">
                        <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center">
                            <i class="ri-phone-line text-teal-600 text-xl"></i>
                        </div>
                        <div>
                            <p class="font-medium text-gray-800">Phone</p>
                            <p class="text-gray-600">+91 98765 43210</p>
                        </div>
                    </div>
                    <div class="flex items-center gap-3">
                        <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center">
                            <i class="ri-mail-line text-teal-600 text-xl"></i>
                        </div>
                        <div>
                            <p class="font-medium text-gray-800">Email</p>
                            <p class="text-gray-600">dr.priya@Hey Dochomoeo.com</p>
                        </div>
                    </div>
                    <div class="flex items-center gap-3">
                        <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center">
                            <i class="ri-map-pin-line text-teal-600 text-xl"></i>
                        </div>
                        <div>
                            <p class="font-medium text-gray-800">Location</p>
                            <p class="text-gray-600">Hyderabad, India</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section class="py-20 bg-gray-50">
        <div class="max-w-6xl mx-auto px-4">
            <h2 class="text-3xl font-bold text-center text-gray-800 mb-12">What Our Patients Say</h2>
            <div class="grid md:grid-cols-3 gap-8">
                <div class="bg-white rounded-lg shadow-md p-6">
                    <div class="flex items-center mb-4">
                        <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center mr-3">
                            <i class="ri-user-line text-teal-600"></i>
                        </div>
                        <div>
                            <h3 class="font-semibold text-gray-800">Rajesh Kumar</h3>
                            <div class="flex text-yellow-400">
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                            </div>
                        </div>
                    </div>
                    <p class="text-gray-600 italic">"Dr. Sharma's homeopathic treatment completely transformed my chronic anxiety. Her compassionate approach and personalized care made all the difference in my healing journey."</p>
                </div>
                
                <div class="bg-white rounded-lg shadow-md p-6">
                    <div class="flex items-center mb-4">
                        <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center mr-3">
                            <i class="ri-user-line text-teal-600"></i>
                        </div>
                        <div>
                            <h3 class="font-semibold text-gray-800">Meera Patel</h3>
                            <div class="flex text-yellow-400">
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                            </div>
                        </div>
                    </div>
                    <p class="text-gray-600 italic">"My daughter's learning difficulties improved significantly under Dr. Sharma's care. The holistic approach addressed not just symptoms but the root cause of her challenges."</p>
                </div>
                
                <div class="bg-white rounded-lg shadow-md p-6">
                    <div class="flex items-center mb-4">
                        <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center mr-3">
                            <i class="ri-user-line text-teal-600"></i>
                        </div>
                        <div>
                            <h3 class="font-semibold text-gray-800">Arjun Singh</h3>
                            <div class="flex text-yellow-400">
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                                <i class="ri-star-fill"></i>
                            </div>
                        </div>
                    </div>
                    <p class="text-gray-600 italic">"Professional, knowledgeable, and genuinely caring. Dr. Sharma's treatment helped me overcome depression naturally without harsh side effects. Highly recommended!"</p>
                </div>
            </div>
        </div>
    </section>

    <section id="contact" class="py-20 bg-white">
        <div class="max-w-6xl mx-auto px-4">
            <h2 class="text-3xl font-bold text-center text-gray-800 mb-12">Contact Us</h2>
            <div class="grid md:grid-cols-2 gap-12">
                <div class="bg-white rounded-lg shadow-md p-8">
                    <h3 class="text-2xl font-semibold text-gray-800 mb-6">Get in Touch</h3>
                    <div class="space-y-6">
                        <div class="flex items-center gap-4">
                            <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-map-pin-line text-teal-600 text-xl"></i>
                            </div>
                            <div>
                                <p class="font-medium text-gray-800">Address</p>
                                <p class="text-gray-600">123 Main Street, Hyderabad, India</p>
                            </div>
                        </div>
                        <div class="flex items-center gap-4">
                            <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-mail-line text-teal-600 text-xl"></i>
                            </div>
                            <div>
                                <p class="font-medium text-gray-800">Email</p>
                                <p class="text-gray-600">info@Hey Dochomoeo.com</p>
                            </div>
                        </div>
                        <div class="flex items-center gap-4">
                            <div class="w-12 h-12 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-phone-line text-teal-600 text-xl"></i>
                            </div>
                            <div>
                                <p class="font-medium text-gray-800">Phone</p>
                                <p class="text-gray-600">+91 12345 67890</p>
                            </div>
                        </div>
                    </div>
                    
                    <div class="mt-8">
                        <h4 class="font-semibold text-gray-800 mb-4">Follow Us</h4>
                        <div class="flex space-x-4">
                            <div class="w-10 h-10 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-facebook-fill text-teal-600"></i>
                            </div>
                            <div class="w-10 h-10 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-twitter-fill text-teal-600"></i>
                            </div>
                            <div class="w-10 h-10 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-instagram-fill text-teal-600"></i>
                            </div>
                            <div class="w-10 h-10 bg-teal-100 rounded-full flex items-center justify-center">
                                <i class="ri-linkedin-fill text-teal-600"></i>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="bg-white rounded-lg shadow-md p-8">
                    <h3 class="text-2xl font-semibold text-gray-800 mb-6">Send Message</h3>
                    <form class="space-y-4">
                        <input type="text" placeholder="Your Name" class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
                        <input type="email" placeholder="Your Email" class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
                        <input type="tel" placeholder="Your Phone" class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
                        <textarea placeholder="Your Message" rows="4" class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"></textarea>
                        <button type="submit" class="w-full bg-teal-600 text-white py-3 rounded-lg font-semibold hover:bg-teal-700 transition-colors">
                            Send Message
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </section>

    <footer class="bg-gray-800 text-white py-12">
        <div class="max-w-6xl mx-auto px-4">
            <div class="grid md:grid-cols-3 gap-8">
                <div>
                    <div class="flex items-center space-x-3 mb-4">
                        <div class="bg-teal-600 text-white p-2 rounded-full">
                            <i class="ri-heart-pulse-line"></i>
                        </div>
                        <span class="text-xl font-bold">Hey Doc!</span>
                    </div>
                    <p class="text-gray-300 mb-4">
                        Providing compassionate homeopathic care with personalized treatment approaches for holistic healing and wellness.
                    </p>
                </div>
                
                <div>
                    <h4 class="text-lg font-semibold mb-4">Quick Links</h4>
                    <ul class="space-y-2">
                        <li><a href="#home" class="text-gray-300 hover:text-white transition-colors">Home</a></li>
                        <li><a href="#doctor" class="text-gray-300 hover:text-white transition-colors">Meet a doctor</a></li>
                        <li><a href="#contact" class="text-gray-300 hover:text-white transition-colors">contact</a></li>
                        
                    </ul>
                </div>
                
                <div>
                    <h4 class="text-lg font-semibold mb-4">Connect With Us</h4>
                    <div class="flex space-x-4 mb-4">
                        <div class="w-10 h-10 bg-gray-700 rounded-full flex items-center justify-center">
                            <i class="ri-facebook-fill text-white"></i>
                        </div>
                        <div class="w-10 h-10 bg-gray-700 rounded-full flex items-center justify-center">
                            <i class="ri-twitter-fill text-white"></i>
                        </div>
                        <div class="w-10 h-10 bg-gray-700 rounded-full flex items-center justify-center">
                            <i class="ri-instagram-fill text-white"></i>
                        </div>
                        <div class="w-10 h-10 bg-gray-700 rounded-full flex items-center justify-center">
                            <i class="ri-linkedin-fill text-white"></i>
                        </div>
                    </div>
                    <p class="text-gray-300 text-sm">© 2024 Hey Doc!. All rights reserved.</p>
                </div>
            </div>
        </div>
    </footer>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const mobileMenuButton = document.getElementById('mobile-menu-button');
            const mobileMenu = document.getElementById('mobile-menu');

            if (mobileMenuButton && mobileMenu) { // Ensure elements exist
                mobileMenuButton.addEventListener('click', function() {
                    mobileMenu.classList.toggle('hidden');
                });

                // Close the mobile menu when a link is clicked (for smoother navigation)
                mobileMenu.querySelectorAll('a').forEach(link => {
                    link.addEventListener('click', () => {
                        mobileMenu.classList.add('hidden');
                    });
                });
            }
        });

        // Function to fully reset the Dialogflow widget (new session)
        function clearChatAndRefresh() {
            try {
                const old = document.querySelector('df-messenger');
                if (!old) return;
                const parent = old.parentNode;

                // Best-effort: clear any cached chat stored by the widget
                try {
                    Object.keys(sessionStorage).forEach(k => {
                        if (k.toLowerCase().includes('df') || k.toLowerCase().includes('dialogflow')) {
                            sessionStorage.removeItem(k);
                        }
                    });
                    Object.keys(localStorage).forEach(k => {
                        if (k.toLowerCase().includes('df') || k.toLowerCase().includes('dialogflow')) {
                            localStorage.removeItem(k);
                        }
                    });
                } catch(_) {}

                // Remove old element first, then recreate after a short delay
                const attrs = Array.from(old.attributes).reduce((acc, a) => { acc[a.name] = a.value; return acc; }, {});
                parent.removeChild(old);
                setTimeout(() => {
                    const fresh = document.createElement('df-messenger');
                    Object.keys(attrs).forEach(name => {
                        if (name.toLowerCase() !== 'session-id') {
                            fresh.setAttribute(name, attrs[name]);
                        }
                    });
                    const newSession = 'session-' + Date.now();
                    fresh.setAttribute('session-id', newSession);

                    const bubble = document.createElement('df-messenger-chat-bubble');
                    bubble.setAttribute('chat-title', 'Hey Doc!');
                    bubble.setAttribute('chat-icon', '/file.jpeg');
                    fresh.appendChild(bubble);

                    parent.appendChild(fresh);
                }, 60);
            } catch (e) {
                console.log('Refresh failed, reloading page as fallback', e);
                location.reload();
            }
        }
    </script>

    <!-- Dialogflow Chatbot -->
    <df-messenger
      location="us-central1"
      project-id="medicare-464710"
      agent-id="4562540a-3955-4572-b455-22b5840e690a"
      language-code="en"
      max-query-length="-1"
      session-id="session-{{ range(1000, 9999) | random }}"
      chat-icon="/file.jpeg">
    <df-messenger-chat-bubble
        chat-title="Hey Doc!"
        chat-icon="/file.jpeg">
    </df-messenger-chat-bubble>
    </df-messenger>
    <style>
      df-messenger {
        z-index: 999;
        position: fixed;
        --df-messenger-font-color: #000;
        --df-messenger-font-family: Google Sans;
        --df-messenger-chat-background: #f3f6fc;
        --df-messenger-message-user-background: #d3e3fd;
        --df-messenger-message-bot-background: #fff;
        bottom: 16px;
        right: 16px;
        transform: scale(0.85);
        transform-origin: bottom right;
      }

      /* Small floating refresh button and starter prompt */
      #df-refresh-btn {
        position: fixed;
        bottom: 84px;  /* sit above bubble */
        right: 28px;
        width: 36px;
        height: 36px;
        border-radius: 9999px;
        background: #10b981; /* teal-500 */
        color: #fff;
        display: flex; align-items: center; justify-content: center;
        box-shadow: 0 4px 10px rgba(0,0,0,0.15);
        cursor: pointer;
      }
      #df-starter-tip {
        position: fixed;
        bottom: 130px;
        right: 28px;
        background: #ffffff;
        color: #111827;
        border: 1px solid #e5e7eb;
        padding: 8px 12px;
        border-radius: 10px;
        box-shadow: 0 6px 20px rgba(0,0,0,0.15);
        max-width: 260px;
      }
    </style>

    <button id="df-refresh-btn" type="button" title="Refresh chat" onclick="clearChatAndRefresh()">⟳</button>
    <div id="df-starter-tip">Hi! Ask about booking, timings, fees, or cancelling an appointment.</div>
    <script>
      // Hide starter tip after a few seconds and when the chat is interacted with
      function hideStarterTip() {
        var tip = document.getElementById('df-starter-tip');
        if (tip) tip.style.display = 'none';
      }
      setTimeout(hideStarterTip, 5000);
      document.addEventListener('click', function(e) {
        const df = document.querySelector('df-messenger');
        if (df && df.contains(e.target)) hideStarterTip();
      });

      // Auto-greet once per session by sending an initial 'hi' to the bot
      document.addEventListener('DOMContentLoaded', function() {
        try {
          const df = document.querySelector('df-messenger');
          if (!df) return;
          const greet = function() {
            if (sessionStorage.getItem('df_greeted') === '1') return;
            sessionStorage.setItem('df_greeted', '1');
            try { if (typeof df.renderCustomText === 'function') df.renderCustomText(''); } catch(_) {}
            try { if (typeof df.sendQuery === 'function') df.sendQuery('hi'); } catch(_) {}
          };
          if (typeof df.sendQuery === 'function') {
            greet();
          } else {
            df.addEventListener('df-messenger-loaded', greet);
          }
        } catch(_) {}
      });
    </script>
</body>
</html>
"""

# Reusable Appointment Form Template (for both Add and Edit)
appointment_form_template = r"""
<!DOCTYPE html>
<html lang="en" class="bg-gray-100">
<head>
  <meta charset="UTF-8">
  <title>{{ 'Add New' if mode == 'add' else 'Edit' }} Appointment - Hey Doc!</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
</head>
<body>
  <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
  <img src="/file.jpeg" alt="Hey Doc Logo" style="height:56px;">
    <h1 class="text-xl font-bold">Hey Doc!- {{ 'Add New' if mode == 'add' else 'Edit' }} Appointment</h1>
    <div>
      <a href="/dashboard" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">Dashboard</a>
      <a href="{{ url_for('logout') }}" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Logout</a>
    </div>
  </nav>

  <div class="p-6">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800">
          {{ message }}
        </div>
      {% endfor %}
    {% endwith %}

    <div class="bg-white rounded-lg shadow-md p-6 max-w-2xl mx-auto">
      <h2 class="text-2xl font-semibold mb-6">{{ 'Add New' if mode == 'add' else 'Edit' }} Appointment</h2>
      
      <form method="POST" action="{{ '/add_appointment' if mode == 'add' else '/edit_appointment/' + appointment_data.appointment_id }}" class="space-y-4">
        {% if mode == 'edit' %}
          <input type="hidden" id="current_appointment_time" value="{{ appointment_data.time }}">
          <input type="hidden" id="current_appointment_date" value="{{ appointment_data.date }}">
          <input type="hidden" id="current_appointment_location" value="{{ appointment_data.location if appointment_data and appointment_data.location else 'Hyderabad' }}">
        {% endif %}
        
        {# Hidden field for appointment_id when editing, to ensure it's passed with form data #}
        {% if mode == 'edit' %}
        <input type="hidden" name="appointment_id" value="{{ appointment_data.appointment_id }}">
        {% endif %}

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label for="name" class="block text-gray-700 font-medium mb-2">Patient Name *</label>
            <input type="text" id="name" name="name" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ appointment_data.name if appointment_data else '' }}">
          </div>
          
          <div>
            <label for="phone" class="block text-gray-700 font-medium mb-2">Phone Number *</label>
            <input type="tel" id="phone" name="phone" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ appointment_data.phone if appointment_data else '' }}">
          </div>
          
          <div>
            <label for="email" class="block text-gray-700 font-medium mb-2">Email</label>
            <input type="email" id="email" name="email"
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ appointment_data.email if appointment_data else '' }}">
          </div>
          
          <div>
            <label for="location" class="block text-gray-700 font-medium mb-2">Location *</label>
            {% if location_options and location_options|length > 0 %}
              <select id="location" name="location" required
                      class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
                {% for city in location_options %}
                  <option value="{{ city }}" {% if appointment_data and appointment_data.location == city %}selected{% endif %}>{{ city }}</option>
                {% endfor %}
              </select>
              <p class="text-xs text-gray-500 mt-1">Showing Branch + City options.</p>
            {% else %}
              <input type="text" id="location" name="location" required
                     class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                     value="{{ appointment_data.location if appointment_data else '' }}" placeholder="Enter city/town">
              <p class="text-xs text-gray-500 mt-1">Must be a real place; validated when loading time slots.</p>
            {% endif %}
          </div>
          
          <div>
            <label for="date" class="block text-gray-700 font-medium mb-2">Appointment Date *</label>
            <input type="date" id="date" name="date" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ appointment_data.date if appointment_data else '' }}"
                   min="{{ today_date }}"> {# Added min attribute here #}
          </div>
          
          <div>
            <label for="time" class="block text-gray-700 font-medium mb-2">Appointment Time *</label>
            <select id="time" name="time" required
        class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
    <option value="" disabled {% if not appointment_data or not appointment_data.time %}selected{% endif %}>Select a time slot</option>
    {% for slot in time_slots %}
        {% set is_booked = slot in booked_slots %}
        <option value="{{ slot }}"
                {% if appointment_data and appointment_data.time == slot %}selected{% endif %}
                {% if is_booked %}disabled style="color: #dc2626; font-weight: bold;"{% else %}style="color: #059669;"{% endif %}>
            {{ slot }}{% if is_booked %} (Booked){% else %} (Available){% endif %}
        </option>
        {% if slot == "11:50" %}
            <option value="" disabled style="color: #f59e42; font-weight: bold;">--- Lunch Break (12:00 - 14:00) ---</option>
        {% endif %}
    {% endfor %}
</select>
<p class="text-sm text-gray-600 mt-1">
    <span class="text-red-600 font-semibold">● Red slots are booked</span> |
    <span class="text-green-600">● Green slots are available</span>
</p>
          </div>
        </div>
        
        <div>
          <label for="address" class="block text-gray-700 font-medium mb-2">Address</label>
          <textarea id="address" name="address" rows="2"
                    class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">{{ appointment_data.address if appointment_data else '' }}</textarea>
        </div>
        
        <div>
          <label for="symptoms" class="block text-gray-700 font-medium mb-2">Symptoms/Reason *</label>
          <textarea id="symptoms" name="symptoms" rows="3" required
                    class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">{{ appointment_data.symptoms if appointment_data else '' }}</textarea>
        </div>
        
        <div class="flex space-x-4">
          <button type="submit" class="bg-teal-600 text-white px-6 py-2 rounded-lg hover:bg-teal-700 transition-colors">
            {{ 'Create Appointment' if mode == 'add' else 'Save Changes' }}
          </button>
          <a href="/dashboard" class="bg-gray-500 text-white px-6 py-2 rounded-lg hover:bg-gray-600 transition-colors">
            Cancel
          </a>
        </div>
      </form>
    </div>
  </div>
  
  <script>
    let ALL_SLOTS_APPT = {{ time_slots | tojson }};

    async function reloadSlotsForCity(city, selectedDate) {
      try {
        const isReal = await validatePlace(city);
        if (!isReal) { throw new Error('Invalid place'); }
        const url = `/get_time_slots?city=${encodeURIComponent(city)}${selectedDate ? `&date=${encodeURIComponent(selectedDate)}` : ''}`;
        const res = await fetch(url);
        const data = await res.json();
        if (data && Array.isArray(data.time_slots)) {
          ALL_SLOTS_APPT = data.time_slots;
        }
      } catch (e) { console.error('Failed to load city slots', e); }
    }

    async function validatePlace(place) {
      try {
        const q = encodeURIComponent(place);
        const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${q}`, { headers: { 'User-Agent': 'clinic-app/1.0' }});
        if (!res.ok) return false;
        const data = await res.json();
        return Array.isArray(data) && data.length > 0;
      } catch (_) { return false; }
    }

    // Function to update time slots based on selected date and city
    async function updateTimeSlots() {
  const dateInput = document.getElementById('date');
  const timeSelect = document.getElementById('time');
  const citySelect = document.getElementById('location');
  const selectedCity = citySelect ? citySelect.value : 'Hyderabad';
  const selectedDate = dateInput.value;
  const curTimeEl = document.getElementById('current_appointment_time');
  const curDateEl = document.getElementById('current_appointment_date');
  const curLocEl = document.getElementById('current_appointment_location');
  const originalTime = (curTimeEl && curDateEl && curDateEl.value === selectedDate && (!curLocEl || curLocEl.value === selectedCity)) ? curTimeEl.value : '';
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const dd = String(now.getDate()).padStart(2, '0');
  const todayStr = `${yyyy}-${mm}-${dd}`;
  const nowHHMM = now.toTimeString().slice(0,5);

  if (!selectedDate) {
    timeSelect.innerHTML = '<option value="">Select date first</option>';
    timeSelect.disabled = true;
    return;
  }

  // Ensure slots match current city and chosen date (for date-specific overrides)
  const ok = await validatePlace(selectedCity);
  if (!ok) {
    timeSelect.innerHTML = '<option value="">Enter a real location</option>';
    timeSelect.disabled = true;
    return;
  }
  await reloadSlotsForCity(selectedCity, selectedDate);

  // Rebuild options
  timeSelect.innerHTML = '<option value="">Select a time slot</option>' +
    ALL_SLOTS_APPT.map(s => `<option value="${s}">${s}</option>`).join('');
  timeSelect.disabled = false;

  // Make AJAX request to get booked slots for the selected date and city
  fetch(`/get_booked_slots/${selectedDate}?city=${encodeURIComponent(selectedCity)}`)
    .then(response => response.json())
    .then(data => {
      let bookedSlots = data.booked_slots || [];
      if (originalTime) {
        bookedSlots = bookedSlots.filter(s => s !== originalTime);
      }
      function normalizeSlot(str) {
    // Remove spaces and make uppercase for reliable comparison
    return str.replace(/\s+/g, '').toUpperCase();
}
      Array.from(timeSelect.options).forEach(option => {
    if (option.value && option.value !== '') {
        const slotTime = option.value;
        // Normalize both for comparison
        const isBookedOrBlocked = bookedSlots.some(
            booked => normalizeSlot(booked) === normalizeSlot(slotTime)
        );
        // ...rest of your logic...
        if (isBookedOrBlocked) {
            option.disabled = true;
            option.style.color = "#dc2626"; // red]
            option.style.fontWeight = "bold";
            option.textContent = slotTime + " (Booked)";
            option.style.display = "";
        } else {
            option.disabled = false;
            option.style.color = "#059669"; // green
            option.style.fontWeight = "bold";
            option.textContent = slotTime + " (Available)";
            option.style.display = "";
        }
    }
});
      if (originalTime) {
        timeSelect.value = originalTime;
      }
    })
    .catch(error => {
      console.error('Error fetching booked slots:', error);
    });
}
    
    // Event listeners
    document.addEventListener('DOMContentLoaded', function() {
      const dateInput = document.getElementById('date');
      const citySelect = document.getElementById('location');
      if (dateInput) {
        dateInput.addEventListener('change', updateTimeSlots);
      }
      if (citySelect) {
        citySelect.addEventListener('change', updateTimeSlots);
      }
      updateTimeSlots();
    });
  </script>
</body>
</html>
"""

# Simple Block Slot Page
block_slot_template = """
<!DOCTYPE html>
<html lang="en" class="bg-gray-100">
<head>
  <meta charset="UTF-8">
  <title>Block Slot - Hey Doc!</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
  <script>
    let ALL_SLOTS_BLOCK = {{ time_slots | tojson }};

    async function reloadBlockSlotsForCity(city) {
      try {
        // When called from fetchUnavailable, we will pass date through that function
        const res = await fetch(`/get_time_slots?city=${encodeURIComponent(city)}`);
        const data = await res.json();
        if (data && Array.isArray(data.time_slots)) {
          ALL_SLOTS_BLOCK = data.time_slots;
          // Rebuild the options quickly; fetchUnavailable will handle disabling
          const select = document.getElementById('b_time');
          if (select) {
            select.innerHTML = '<option value="">Select time</option>' + ALL_SLOTS_BLOCK.map(s => `<option value="${s}">${s}</option>`).join('');
          }
        }
      } catch (e) { console.error('Failed to load block slots for city', e); }
    }

    function fetchUnavailable() {
      const date = document.getElementById('b_date').value;
      const select = document.getElementById('b_time');
      const citySel = document.getElementById('b_location');
      const city = citySel ? citySel.value : 'Hyderabad';
      const now = new Date();
      const yyyy = now.getFullYear();
      const mm = String(now.getMonth() + 1).padStart(2, '0');
      const dd = String(now.getDate()).padStart(2, '0');
      const todayStr = `${yyyy}-${mm}-${dd}`;
      const nowHHMM = now.toTimeString().slice(0,5);
      if (!date) {
        // No date chosen yet
        select.innerHTML = '<option value="">Select date first</option>';
        select.disabled = true;
        return;
      }

      // Reload slots for city+date to respect date-specific overrides
      fetch(`/get_time_slots?city=${encodeURIComponent(city)}&date=${encodeURIComponent(date)}`)
        .then(r => r.json())
        .then(data => {
          if (data && Array.isArray(data.time_slots)) {
            ALL_SLOTS_BLOCK = data.time_slots;
          }
          select.innerHTML = '<option value="">Select time</option>' + ALL_SLOTS_BLOCK.map(s => `<option value="${s}">${s}</option>`).join('');
          select.disabled = false;
        })
        .catch(() => {
          select.innerHTML = '<option value="">Select time</option>' + ALL_SLOTS_BLOCK.map(s => `<option value="${s}">${s}</option>`).join('');
          select.disabled = false;
        });
      fetch(`/get_booked_slots/${date}?city=${encodeURIComponent(city)}`)
        .then(r => r.json())
        .then(data => {
          const unavailable = data.booked_slots || [];
          Array.from(select.options).forEach(opt => {
            if (!opt.value) return;
            const isUnavailable = unavailable.includes(opt.value);
            const isPastToday = (date === todayStr) && (opt.value < nowHHMM);

            if (isUnavailable) {
              opt.disabled = true;
              opt.textContent = opt.value + ' (Unavailable)';
              opt.style.display = '';
            } else if (isPastToday) {
              opt.disabled = true;
              opt.textContent = opt.value + ' (Past)';
              opt.style.display = 'none';
            } else {
              opt.disabled = false;
              opt.textContent = opt.value;
              opt.style.display = '';
            }
          });
        });
    }
    document.addEventListener('DOMContentLoaded', function() {
      const locSel = document.getElementById('b_location');
      if (locSel) {
        locSel.addEventListener('change', async function() {
          const date = document.getElementById('b_date') ? document.getElementById('b_date').value : '';
          // Preload slots for city+date
          try { await fetch(`/get_time_slots?city=${encodeURIComponent(this.value)}${date ? `&date=${encodeURIComponent(date)}` : ''}`); } catch(e) {}
          fetchUnavailable();
        });
      }
    });
  </script>
</head>
<body>
  <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
    <h1 class="text-xl font-bold">Block a Slot</h1>
    <div>
      <a href="/dashboard" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Dashboard</a>
    </div>
  </nav>
  <div class="p-6 max-w-xl mx-auto">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800">{{ message }}</div>
      {% endfor %}
    {% endwith %}

    <div class="bg-white rounded-lg shadow p-6">
      <form method="POST" action="/block_slot" class="space-y-4">
        <div>
          <label class="block text-gray-700 font-medium mb-2">Date</label>
          <input type="date" id="b_date" name="date" class="professional-input w-full" required onchange="fetchUnavailable()" min="{{ datetime.utcnow().strftime('%Y-%m-%d') }}">
        </div>
        <div>
          <label class="block text-gray-700 font-medium mb-2">Location</label>
          <select id="b_location" name="location" class="professional-select w-full" onchange="fetchUnavailable()" required>
            {% for city in available_cities %}
            <option value="{{ city }}">{{ city }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="block text-gray-700 font-medium mb-2">Time</label>
          <select id="b_time" name="time" class="professional-select w-full" required>
            <option value="">Select time</option>
            {% for slot in time_slots %}
            <option value="{{ slot }}">{{ slot }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="block text-gray-700 font-medium mb-2">Reason (optional)</label>
          <input type="text" name="reason" class="professional-input w-full" placeholder="Personal, Surgery, Meeting...">
        </div>
        <div class="flex space-x-3">
          <button type="submit" class="bg-teal-600 text-white px-6 py-2 rounded-lg hover:bg-teal-700">Block Slot</button>
          <a href="/dashboard" class="bg-gray-500 text-white px-6 py-2 rounded-lg hover:bg-gray-600">Cancel</a>
        </div>
      </form>
    </div>

    <div class="bg-white rounded-lg shadow p-6 mt-6">
      <h2 class="text-lg font-semibold mb-4">Currently Blocked Slots (Upcoming)</h2>
      <ul class="list-disc pl-5 space-y-2">
        {% for s in blocked_list %}
          <li>{{ s.date }} {{ s.time }}{% if s.reason %} - {{ s.reason }}{% endif %}
            <a class="text-red-600 ml-2" href="/unblock_slot?id={{ s._id }}">Unblock</a>
          </li>
        {% else %}
          <li class="text-gray-600">No blocked slots</li>
        {% endfor %}
      </ul>
    </div>
  </div>
</body>
</html>
"""

# Availability Form Template
availability_form_template = """
<!DOCTYPE html>
<html lang="en" class="bg-gray-100">
<head>
  <meta charset="UTF-8">
  <title>Add Availability - Hey Doc!</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
  <script>
    function onModeChange() {
      const mode = document.querySelector('input[name="mode"]:checked').value;
      const dateRow = document.getElementById('date_row');
      if (mode === 'date') { dateRow.classList.remove('hidden'); } else { dateRow.classList.add('hidden'); }
    }
    document.addEventListener('DOMContentLoaded', onModeChange);
  </script>
  <style>
    .professional-input { width: 100%; padding: 0.5rem 1rem; border: 1px solid #d1d5db; border-radius: 0.5rem; }
    .professional-select { width: 100%; padding: 0.5rem 1rem; border: 1px solid #d1d5db; border-radius: 0.5rem; }
    .section-title { font-size: 1.125rem; font-weight: 600; color: #1f2937; margin-bottom: 0.5rem; }
  </style>
</head>
<body>
  <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
    <h1 class="text-xl font-bold">Add Availability</h1>
    <div>
      <a href="/dashboard" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Dashboard</a>
    </div>
  </nav>

  <div class="p-6 max-w-3xl mx-auto">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800">{{ message }}</div>
      {% endfor %}
    {% endwith %}

    <div class="bg-white rounded-lg shadow p-6">
      <form method="POST" action="/add_availability" class="space-y-6">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label class="block text-gray-700 font-medium mb-2">Hospital Name</label>
            <input type="text" name="hospital_name" class="professional-input" value="Hey Doc!" placeholder="Hospital/Clinic name">
          </div>
          <div>
            <label class="block text-gray-700 font-medium mb-2">Location</label>
            {% if location_options and location_options|length > 0 %}
              <select name="location" class="professional-select" required>
                {% for city in location_options %}
                  <option value="{{ city }}">{{ city }}</option>
                {% endfor %}
              </select>
              <p class="text-xs text-gray-500 mt-1">Locations from Branch + City list.</p>
            {% else %}
              <input type="text" name="location" class="professional-input" placeholder="Akola, Hyderabad or Pune" list="cities" required>
              <datalist id="cities">
                {% for city in available_cities %}
                <option value="{{ city }}"></option>
                {% endfor %}
              </datalist>
              <p class="text-xs text-gray-500 mt-1">Only real clinic locations are accepted.</p>
            {% endif %}
          </div>
        </div>

        <div>
          <label class="block text-gray-700 font-medium mb-2">Document Mode</label>
          <div class="flex items-center space-x-6">
            <label class="inline-flex items-center space-x-2">
              <input type="radio" name="mode" value="default" checked onchange="onModeChange()">
              <span>Default (applies to all dates)</span>
            </label>
            <label class="inline-flex items-center space-x-2">
              <input type="radio" name="mode" value="date" onchange="onModeChange()">
              <span>Date-specific override</span>
            </label>
          </div>
        </div>

        <div id="date_row" class="hidden">
          <label class="block text-gray-700 font-medium mb-2">Date</label>
          <input type="date" name="date" class="professional-input" min="{{ datetime.utcnow().strftime('%Y-%m-%d') }}">
          <p class="text-sm text-gray-500 mt-1">If set, this availability will only apply to the selected date.</p>
        </div>

        <div>
          <h3 class="section-title">Working Hours</h3>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="border rounded-lg p-4">
              <h4 class="font-medium text-gray-800 mb-3">Morning Shift</h4>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-gray-700 text-sm mb-1">Start</label>
                  <input type="time" name="morning_start" class="professional-input" placeholder="hh:mm">
                </div>
                <div>
                  <label class="block text-gray-700 text-sm mb-1">End</label>
                  <input type="time" name="morning_end" class="professional-input" placeholder="hh:mm">
                </div>
              </div>
            </div>
            
            <div class="border rounded-lg p-4 md:col-span-2">
              <h4 class="font-medium text-gray-800 mb-3">Evening Shift</h4>
              <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div>
                  <label class="block text-gray-700 text-sm mb-1">Start</label>
                  <input type="time" name="evening_start" class="professional-input" placeholder="hh:mm">
                </div>
                <div>
                  <label class="block text-gray-700 text-sm mb-1">End</label>
                  <input type="time" name="evening_end" class="professional-input" placeholder="hh:mm">
                </div>
              </div>
            </div>
          </div>
          <p class="text-sm text-gray-500 mt-2">Enter at least one shift with both start and end times.</p>
        </div>

        <div class="flex space-x-3">
          <button type="submit" class="bg-teal-600 text-white px-6 py-2 rounded-lg hover:bg-teal-700">Save Availability</button>
          <a href="/dashboard" class="bg-gray-500 text-white px-6 py-2 rounded-lg hover:bg-gray-600">Cancel</a>
        </div>
      </form>
    </div>
  </div>
</body>
</html>
"""

# Prescription Form Template
prescription_form_template = """
<!DOCTYPE html>
<html lang="en" class="bg-gray-100">
<head>
  <meta charset="UTF-8">
  <title>Add Prescription - Hey Doc!</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
</head>
<body>
  <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
    <h1 class="text-xl font-bold">Hey Doc! - Add Prescription</h1>
    <div>
      <a href="/dashboard" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">Dashboard</a>
      <a href="/prescriptions" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">View Prescriptions</a>
      <a href="{{ url_for('logout') }}" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Logout</a>
    </div>
  </nav>

  <div class="p-6">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800">
          {{ message }}
        </div>
      {% endfor %}
    {% endwith %}

    <div class="bg-white rounded-lg shadow-md p-6 max-w-4xl mx-auto">
      <h2 class="text-2xl font-semibold mb-6">Add New Prescription</h2>
      
      <form method="POST" action="/add_prescription" class="space-y-6">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <label for="patient_name" class="block text-gray-700 font-medium mb-2">Patient Name *</label>
            <input type="text" id="patient_name" name="patient_name" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ prescription_data.patient_name if prescription_data else '' }}">
          </div>
          
          <div>
            <label for="patient_phone" class="block text-gray-700 font-medium mb-2">Patient Phone *</label>
            <input type="tel" id="patient_phone" name="patient_phone" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ prescription_data.patient_phone if prescription_data else '' }}">
          </div>
          
          <div>
            <label for="prescription_date" class="block text-gray-700 font-medium mb-2">Prescription Date *</label>
            <input type="date" id="prescription_date" name="prescription_date" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ prescription_data.prescription_date_iso if prescription_data and prescription_data.prescription_date_iso else today_date }}">
          </div>
          
          <div>
            <label for="diagnosis" class="block text-gray-700 font-medium mb-2">Diagnosis *</label>
            <input type="text" id="diagnosis" name="diagnosis" required
                   class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                   value="{{ prescription_data.diagnosis if prescription_data else '' }}">
          </div>
        </div>
        
        <div>
          <label for="medicines" class="block text-gray-700 font-medium mb-2">Medicines *</label>
          <div id="medicines-container" class="space-y-4">
            <div class="medicine-entry border border-gray-200 rounded-lg p-4">
              <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                <div>
                  <label class="block text-gray-700 text-sm font-medium mb-1">Medicine Name</label>
                  <input type="text" name="medicine_names[]" required
                         class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                         placeholder="e.g., Arnica Montana">
                </div>
                <div>
                  <label class="block text-gray-700 text-sm font-medium mb-1">Potency</label>
                  <input type="text" name="potencies[]" required
                         class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                         placeholder="e.g., 30C">
                </div>
                <div>
                  <label class="block text-gray-700 text-sm font-medium mb-1">Dosage</label>
                  <input type="text" name="dosages[]" required
                         class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                         placeholder="e.g., 3 times daily">
                </div>
                <div>
                  <label class="block text-gray-700 text-sm font-medium mb-1">Duration</label>
                  <input type="text" name="durations[]" required
                         class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                         placeholder="e.g., 7 days">
                </div>
              </div>
            </div>
          </div>
          <button type="button" id="add-medicine" class="mt-2 bg-blue-500 text-white px-4 py-2 rounded hover:bg-blue-600 transition-colors">
            <i class="ri-add-line mr-1"></i>Add Another Medicine
          </button>
        </div>
        
        <div>
          <label for="instructions" class="block text-gray-700 font-medium mb-2">Special Instructions</label>
          <textarea id="instructions" name="instructions" rows="3"
                    class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                    placeholder="Any special instructions for the patient...">{{ prescription_data.instructions if prescription_data else '' }}</textarea>
        </div>
        
        <div>
          <label for="notes" class="block text-gray-700 font-medium mb-2">Doctor's Notes</label>
          <textarea id="notes" name="notes" rows="3"
                    class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
                    placeholder="Additional notes...">{{ prescription_data.notes if prescription_data else '' }}</textarea>
        </div>
        
        <div class="flex space-x-4">
          <button type="submit" class="bg-teal-600 text-white px-6 py-2 rounded-lg hover:bg-teal-700 transition-colors">
            Save Prescription
          </button>
          <a href="/prescriptions" class="bg-gray-500 text-white px-6 py-2 rounded-lg hover:bg-gray-600 transition-colors">
            Cancel
          </a>
        </div>
      </form>
    </div>
  </div>
  
  <script>
    document.addEventListener('DOMContentLoaded', function() {
      const addMedicineBtn = document.getElementById('add-medicine');
      const medicinesContainer = document.getElementById('medicines-container');
      
      addMedicineBtn.addEventListener('click', function() {
        const medicineEntry = document.createElement('div');
        medicineEntry.className = 'medicine-entry border border-gray-200 rounded-lg p-4';
        medicineEntry.innerHTML = `
          <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div>
              <label class="block text-gray-700 text-sm font-medium mb-1">Medicine Name</label>
              <input type="text" name="medicine_names[]" required
                     class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                     placeholder="e.g., Arnica Montana">
            </div>
            <div>
              <label class="block text-gray-700 text-sm font-medium mb-1">Potency</label>
              <input type="text" name="potencies[]" required
                     class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                     placeholder="e.g., 30C">
            </div>
            <div>
              <label class="block text-gray-700 text-sm font-medium mb-1">Dosage</label>
              <input type="text" name="dosages[]" required
                     class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                     placeholder="e.g., 3 times daily">
            </div>
            <div class="flex items-end">
              <div class="flex-1">
                <label class="block text-gray-700 text-sm font-medium mb-1">Duration</label>
                <input type="text" name="durations[]" required
                       class="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500"
                       placeholder="e.g., 7 days">
              </div>
              <button type="button" class="ml-2 bg-red-500 text-white px-3 py-2 rounded hover:bg-red-600 transition-colors remove-medicine">
                <i class="ri-delete-bin-line"></i>
              </button>
            </div>
          </div>
        `;
        
        medicinesContainer.appendChild(medicineEntry);
        
        // Add remove functionality to the new entry
        const removeBtn = medicineEntry.querySelector('.remove-medicine');
        removeBtn.addEventListener('click', function() {
          medicineEntry.remove();
        });
      });
      
      // Add remove functionality to the first entry
      const firstRemoveBtn = medicinesContainer.querySelector('.remove-medicine');
      if (firstRemoveBtn) {
        firstRemoveBtn.addEventListener('click', function() {
          medicinesContainer.querySelector('.medicine-entry').remove();
        });
      }
    });
  </script>
</body>
</html>
"""

# Prescription History Template
prescription_history_template = """
<!DOCTYPE html>
<html lang="en" class="bg-gray-100">
<head>
  <meta charset="UTF-8">
  <title>Prescription History - Hey Doc!</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
</head>
<body>
  <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
    <h1 class="text-xl font-bold">Hey Doc! - Prescription History</h1>
    <div>
      <a href="/dashboard" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">Dashboard</a>
      <a href="/add_prescription" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">Add Prescription</a>
      <a href="{{ url_for('logout') }}" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Logout</a>
    </div>
  </nav>

  <div class="p-6">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800">
          {{ message }}
        </div>
      {% endfor %}
    {% endwith %}

    <div class="bg-white rounded-lg shadow-md p-6">
      <div class="flex justify-between items-center mb-6">
        <h2 class="text-2xl font-semibold">
          {% if patient_phone %}
            {% if patient_name %}
              Prescriptions for Patient: {{ patient_name }} ({{ patient_phone }})
            {% else %}
              Prescriptions for Patient: {{ patient_phone }}
            {% endif %}
          {% else %}
            Prescription History
          {% endif %}
        </h2>
        <div class="flex space-x-2">
          {% if patient_phone %}
            <a href="/prescriptions" class="bg-gray-600 text-white px-4 py-2 rounded-lg hover:bg-gray-700 transition-colors">
              <i class="ri-list-check mr-1"></i>View All Prescriptions
            </a>
          {% endif %}
          <a href="/add_prescription{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">
            <i class="ri-add-line mr-1"></i>Add New Prescription
          </a>
        </div>
      </div>

      <form method="GET" action="/prescriptions" class="mb-6 flex flex-col md:flex-row items-center space-y-2 md:space-y-0 md:space-x-4">
        {% if patient_phone %}
          <input type="hidden" name="patient_phone" value="{{ patient_phone }}">
        {% endif %}
        <input type="text" name="search_query" placeholder="Search by Patient Name or Phone..." 
               class="flex-grow w-full md:w-auto px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
               value="{{ search_query if search_query else '' }}">
        <button type="submit" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">
          <i class="ri-search-line mr-1"></i>Search
        </button>
        {% if search_query %}
          <a href="/prescriptions{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-lg hover:bg-gray-400 transition-colors">Clear Search</a>
        {% endif %}

        <div class="flex items-center space-x-2 w-full md:w-auto">
          <label for="sort_by" class="text-gray-700">Sort by:</label>
          <select id="sort_by" name="sort_by" class="px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
            <option value="">Default (Latest First)</option>
            <option value="patient_name_asc" {% if sort_by == 'patient_name_asc' %}selected{% endif %}>Patient Name (A-Z)</option>
            <option value="patient_name_desc" {% if sort_by == 'patient_name_desc' %}selected{% endif %}>Patient Name (Z-A)</option>
            <option value="date_asc" {% if sort_by == 'date_asc' %}selected{% endif %}>Date (Oldest First)</option>
            <option value="date_desc" {% if sort_by == 'date_desc' %}selected{% endif %}>Date (Newest First)</option>
          </select>
          <button type="submit" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">
            Sort
          </button>
        </div>
      </form>

      <div class="space-y-6">
        {% for prescription in prescriptions %}
        <div class="border border-gray-200 rounded-lg p-6 hover:shadow-md transition-shadow">
          <div class="flex justify-between items-start mb-4">
            <div>
              <h3 class="text-xl font-semibold text-gray-800">{{ prescription.patient_name }}</h3>
              <p class="text-gray-600">{{ prescription.patient_phone }}</p>
              <p class="text-sm text-gray-500">Prescription Date: {{ prescription.prescription_date }}</p>
              <p class="text-sm text-gray-500">Prescription ID: {{ prescription.prescription_id }}</p>
            </div>
            <div class="text-right">
              <span class="bg-teal-100 text-teal-800 px-3 py-1 rounded-full text-sm font-medium">
                {{ prescription.created_at_str }}
              </span>
            </div>
          </div>
          
          <div class="grid md:grid-cols-2 gap-6 mb-4">
            <div>
              <h4 class="font-semibold text-gray-700 mb-2">Diagnosis</h4>
              <p class="text-gray-600">{{ prescription.diagnosis }}</p>
            </div>
            <div>
              <h4 class="font-semibold text-gray-700 mb-2">Special Instructions</h4>
              <p class="text-gray-600">{{ prescription.instructions or 'None' }}</p>
            </div>
          </div>
          
          <div class="mb-4">
            <h4 class="font-semibold text-gray-700 mb-3">Medicines</h4>
            <div class="bg-gray-50 rounded-lg p-4">
              {% for medicine in prescription.medicines %}
              <div class="border-b border-gray-200 pb-3 mb-3 last:border-b-0 last:pb-0 last:mb-0">
                <div class="grid grid-cols-1 md:grid-cols-4 gap-4 text-sm">
                  <div>
                    <span class="font-medium text-gray-700">Medicine:</span>
                    <p class="text-gray-600">{{ medicine.name }}</p>
                  </div>
                  <div>
                    <span class="font-medium text-gray-700">Potency:</span>
                    <p class="text-gray-600">{{ medicine.potency }}</p>
                  </div>
                  <div>
                    <span class="font-medium text-gray-700">Dosage:</span>
                    <p class="text-gray-600">{{ medicine.dosage }}</p>
                  </div>
                  <div>
                    <span class="font-medium text-gray-700">Duration:</span>
                    <p class="text-gray-600">{{ medicine.duration }}</p>
                  </div>
                </div>
              </div>
              {% endfor %}
            </div>
          </div>
          
          {% if prescription.notes %}
          <div class="mb-4">
            <h4 class="font-semibold text-gray-700 mb-2">Doctor's Notes</h4>
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4">
              <p class="text-gray-700">{{ prescription.notes }}</p>
            </div>
          </div>
          {% endif %}
          
          <div class="flex justify-end space-x-2">
            <a href="/view_prescription/{{ prescription.prescription_id }}{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" 
               class="bg-blue-500 text-white px-4 py-2 rounded hover:bg-blue-600 transition-colors text-sm">
              <i class="ri-eye-line mr-1"></i>View Details
            </a>
            <a href="/print_prescription/{{ prescription.prescription_id }}{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" 
               class="bg-green-500 text-white px-4 py-2 rounded hover:bg-green-600 transition-colors text-sm">
              <i class="ri-printer-line mr-1"></i>Print
            </a>
            <a href="/delete_prescription/{{ prescription.prescription_id }}{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" 
               class="bg-red-500 text-white px-4 py-2 rounded hover:bg-red-600 transition-colors text-sm"
               onclick="return confirm('Are you sure you want to delete this prescription? This action cannot be undone.')">
              <i class="ri-delete-bin-line mr-1"></i>Delete
            </a>
          </div>
        </div>
        {% endfor %}
        
        {% if not prescriptions %}
        <div class="text-center py-12">
          <div class="text-gray-400 mb-4">
            <i class="ri-medicine-bottle-line text-6xl"></i>
          </div>
          <h3 class="text-xl font-semibold text-gray-600 mb-2">
            {% if patient_phone %}
              {% if patient_name %}
                No Prescriptions Found for Patient: {{ patient_name }} ({{ patient_phone }})
              {% else %}
                No Prescriptions Found for Patient: {{ patient_phone }}
              {% endif %}
            {% else %}
              No Prescriptions Found
            {% endif %}
          </h3>
          <p class="text-gray-500 mb-4">
            {% if patient_phone %}
              This patient doesn't have any prescriptions yet.
            {% else %}
              Start by adding your first prescription.
            {% endif %}
          </p>
          <a href="/add_prescription{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" class="bg-teal-600 text-white px-6 py-3 rounded-lg hover:bg-teal-700 transition-colors">
            {% if patient_phone %}
              Add Prescription for This Patient
            {% else %}
              Add First Prescription
            {% endif %}
          </a>
        </div>
        {% endif %}
      </div>
    </div>
  </div>

  <!-- Dialogflow Chatbot -->
  <df-messenger
    location="us-central1"
    project-id="medicare-464710"
    agent-id="4562540a-3955-4572-b455-22b5840e690a"
    language-code="en"
    max-query-length="-1"
    session-id="session-{{ range(1000, 9999) | random }}"
    chat-icon="/file.jpeg">
  <df-messenger-chat-bubble
      chat-title="Hey Doc!"
      chat-icon="/file.jpeg">
  </df-messenger-chat-bubble>
  </df-messenger>
  <style>
    df-messenger {
      z-index: 999;
      position: fixed;
      --df-messenger-font-color: #000;
      --df-messenger-font-family: Google Sans;
      --df-messenger-chat-background: #f3f6fc;
      --df-messenger-message-user-background: #d3e3fd;
      --df-messenger-message-bot-background: #fff;
      bottom: 16px;
      right: 16px;
      width: 280px;             /* smaller container */
      height: 400px;            /* smaller height */
      max-height: calc(100vh - 88px - 16px); /* keep clear of fixed navbar */
    }
    @media (max-width: 640px) {
      df-messenger {
        width: calc(100vw - 24px);
        height: 60vh;
        max-height: calc(100vh - 72px - 12px);
        right: 12px;
        bottom: 12px;
      }
    }

    /* Removed refresh button and starter tip styles */
  </style>

  <!-- Removed refresh button and starter tip scripts -->
  <script>
    // Auto-greet once per session on this page
    document.addEventListener('DOMContentLoaded', function() {
      try {
        const df = document.querySelector('df-messenger');
        if (!df) return;
        const greet = function() {
          if (sessionStorage.getItem('df_greeted') === '1') return;
          sessionStorage.setItem('df_greeted', '1');
          try { if (typeof df.renderCustomText === 'function') df.renderCustomText(''); } catch(_) {}
          try { if (typeof df.sendQuery === 'function') df.sendQuery('hi'); } catch(_) {}
        };
        if (typeof df.sendQuery === 'function') {
          greet();
        } else {
          df.addEventListener('df-messenger-loaded', greet);
        }
      } catch(_) {}
    });
  </script>
</body>
</html>
"""

dashboard_template = """
<!DOCTYPE html>
<html lang="en" class="bg-gray-100">
<head>
  <meta charset="UTF-8">
  <title>Dashboard - Hey Doc!</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
  <link rel="stylesheet" href="https://www.gstatic.com/dialogflow-console/fast/df-messenger/prod/v1/themes/df-messenger-default.css">
  <script src="https://www.gstatic.com/dialogflow-console/fast/df-messenger/prod/v1/df-messenger.js"></script>
</head>
<body>
  <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
    <h1 class="text-xl font-bold">Hey Doc! - Dashboard</h1>
    <div>
      <span class="mr-4">Welcome, Dr. {{ doctor }}</span>
      <a href="{{ url_for('logout') }}" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Logout</a>
    </div>
  </nav>

  <div class="p-6">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800">
          {{ message }}
        </div>
      {% endfor %}
    {% endwith %}

    <div class="bg-white rounded-lg shadow-md p-4">
              <div class="flex justify-between items-center mb-4">
        <h2 class="text-lg font-semibold">Appointment Records</h2>
        <div class="flex space-x-2">
          <a href="/calendar" class="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 transition-colors">
            <i class="ri-calendar-line mr-1"></i>Calendar View
          </a>
          <a href="/add_appointment" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">
            <i class="ri-add-line mr-1"></i>Add Appointment
          </a>
          <a href="/prescriptions" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition-colors">
            <i class="ri-medicine-bottle-line mr-1"></i>Prescriptions
          </a>
          <a href="/add_availability" class="bg-orange-600 text-white px-4 py-2 rounded-lg hover:bg-orange-700 transition-colors">
            <i class="ri-time-line mr-1"></i>Add Availability
          </a>
          <a href="/add_branch" class="bg-indigo-600 text-white px-4 py-2 rounded-lg hover:bg-indigo-700 transition-colors">
            <i class="ri-building-2-line mr-1"></i>Add Branch
          </a>
        </div>
      </div>

      <form method="GET" action="/dashboard" class="mb-6 flex flex-col md:flex-row items-center space-y-2 md:space-y-0 md:space-x-4">
        <input type="text" name="search_query" placeholder="Search by Name or Appointment ID..." 
               class="flex-grow w-full md:w-auto px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500"
               value="{{ search_query if search_query else '' }}">
        <button type="submit" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">
          <i class="ri-search-line mr-1"></i>Search
        </button>
        {% if search_query %}
          <a href="/dashboard" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-lg hover:bg-gray-400 transition-colors">Clear Search</a>
        {% endif %}

        <div class="flex items-center space-x-2 w-full md:w-auto">
          <label for="sort_by" class="text-gray-700">Sort by:</label>
          <select id="sort_by" name="sort_by" class="px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:border-teal-500">
            <option value="">Default (Latest Created)</option>
            <option value="name_asc" {% if sort_by == 'name_asc' %}selected{% endif %}>Patient Name (A-Z)</option>
            <option value="name_desc" {% if sort_by == 'name_desc' %}selected{% endif %}>Patient Name (Z-A)</option>
            <option value="date_asc" {% if sort_by == 'date_asc' %}selected{% endif %}>Appointment Date (Oldest First)</option>
            <option value="date_desc" {% if sort_by == 'date_desc' %}selected{% endif %}>Appointment Date (Newest First)</option>
          </select>
          <button type="submit" class="bg-teal-600 text-white px-4 py-2 rounded-lg hover:bg-teal-700 transition-colors">
            Sort
          </button>
        </div>
      </form>


      <div class="overflow-x-auto">
        <div class="max-h-[600px] overflow-y-auto border rounded-lg shadow-inner"> 
          <table class="w-full text-sm text-left">
            <thead class="bg-teal-100 text-teal-800 sticky top-0 bg-teal-100 z-10"> 
              <tr>
                <th class="p-2 border">Appointment ID</th>
                <th class="p-2 border">Name</th>
                <th class="p-2 border">Phone</th>
                <th class="p-2 border">Email</th>
                <th class="p-2 border">Address</th>
                <th class="p-2 border">Symptoms</th>
                <th class="p-2 border">Date</th>
                <th class="p-2 border">Time</th>
                <th class="p-2 border">Status</th>
                <th class="p-2 border">Created At</th>
                <th class="p-2 border">Actions</th>
              </tr>
            </thead>
            <tbody>
              {% for appointment in appointments %}
                <tr class="hover:bg-teal-50">
                  <td class="p-2 border">{{ appointment.get('appointment_id', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('name', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('phone', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('email', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('address', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('symptoms', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('date', 'N/A') }}</td>
                  <td class="p-2 border">{{ appointment.get('time', 'N/A') }}</td>
                  <td class="p-2 border">
                    <span class="px-2 py-1 rounded text-xs font-medium 
                      {% if appointment.get('status') == 'confirmed' %}bg-green-100 text-green-800
                      {% elif appointment.get('status') == 'pending' %}bg-yellow-100 text-yellow-800
                      {% elif appointment.get('status') == 'cancelled' %}bg-red-100 text-red-800
                      {% else %}bg-gray-100 text-gray-800{% endif %}">
                      {{ appointment.get('status', 'N/A') }}
                    </span>
                  </td>
                  <td class="p-2 border">{{ appointment.get('created_at_str', 'N/A') }}</td>
                  <td class="p-2 border">
                    <div class="flex flex-col space-y-1"> 
                      {% if appointment.get('status') != 'confirmed' %}
                      <a href="/update_appointment_status/{{ appointment.get('appointment_id', '') }}/confirmed" 
                         class="bg-green-500 text-white px-2 py-1 rounded text-xs hover:bg-green-600 text-center"
                         onclick="return confirm('Confirm this appointment?')">
                        Confirm
                      </a>
                      {% endif %}
                      {% if appointment.get('status') != 'cancelled' %}
                      <a href="/update_appointment_status/{{ appointment.get('appointment_id', '') }}/cancelled" 
                         class="bg-red-500 text-white px-2 py-1 rounded text-xs hover:bg-red-600 text-center"
                         onclick="return confirm('Cancel this appointment?')">
                        Cancel
                      </a>
                      {% endif %}

                      {# New Edit Button - Only show if not cancelled #}
                      {% if appointment.get('status') != 'cancelled' %}
                      <a href="/edit_appointment/{{ appointment.get('appointment_id', '') }}" 
                         class="bg-blue-500 text-white px-2 py-1 rounded text-xs hover:bg-blue-600 text-center">
                        Edit
                      </a>
                      {% endif %}
                      {# View Prescriptions Button - Only show if not cancelled #}
                      {% if appointment.get('status') != 'cancelled' %}
                      <a href="/prescriptions?patient_phone={{ appointment.get('phone', '') }}" 
                         class="bg-purple-500 text-white px-2 py-1 rounded text-xs hover:bg-purple-600 text-center"
                         title="View prescriptions for {{ appointment.get('name', '') }}">
                        <i class="ri-medicine-bottle-line mr-1"></i>Prescriptions
                      </a>
                      {% endif %}
                    </div>
                  </td>
                </tr>
              {% endfor %}
              {% if not appointments %}
              <tr>
                <td colspan="11" class="p-4 text-center text-gray-500">No appointments found.</td>
              </tr>
              {% endif %}
            </tbody>
          </table>
        </div> {# End of max-height div #}
      </div>
    </div>
  </div>
</body>
</html>

"""

# --- Routes ---
@app.route("/")
def home():
    return render_template_string(home_template)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        doctor = doctors_collection.find_one({"username": username, "password": password})
        if doctor:
            session["doctor"] = username
            flash("Logged in successfully!", "success")
            return redirect("/dashboard")
        else:
            flash("Invalid username or password", "error")
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en" class="bg-gray-100">
                <head>
                    <meta charset="UTF-8">
                    <title>Doctor Login</title>
                    <script src="https://cdn.tailwindcss.com"></script>
                </head>
                <body class="flex items-center justify-center min-h-screen bg-gray-100">
                    <div class="bg-white p-8 rounded-lg shadow-md w-full max-w-sm">
                        <h2 class="text-2xl font-bold mb-6 text-center text-gray-800">Doctor Login</h2>
                        {% with messages = get_flashed_messages(with_categories=true) %}
                            {% for category, message in messages %}
                                <div class="mb-4 text-sm p-3 rounded bg-red-100 text-red-800">
                                    {{ message }}
                                </div>
                            {% endfor %}
                        {% endwith %}
                        <form method="POST" action="/login">
                            <div class="mb-4">
                                <label for="username" class="block text-gray-700 text-sm font-bold mb-2">Username:</label>
                                <input type="text" id="username" name="username" required
                                       class="shadow appearance-none border rounded w-full py-2 px-3 text-gray-700 leading-tight focus:outline-none focus:shadow-outline">
                            </div>
                            <div class="mb-6">
                                <label for="password" class="block text-gray-700 text-sm font-bold mb-2">Password:</label>
                                <input type="password" id="password" name="password" required
                                       class="shadow appearance-none border rounded w-full py-2 px-3 text-gray-700 mb-3 leading-tight focus:outline-none focus:shadow-outline">
                            </div>
                            <div class="flex items-center justify-between">
                                <button type="submit"
                                        class="bg-teal-600 hover:bg-teal-700 text-white font-bold py-2 px-4 rounded focus:outline-none focus:shadow-outline">
                                    Login
                                </button>
                                <a href="/" class="inline-block align-baseline font-bold text-sm text-teal-600 hover:text-teal-800">
                                    Back to Home
                                </a>
                            </div>
                        </form>
                    </div>
                </body>
                </html>
            """)
    
    if "doctor" in session:
        return redirect("/dashboard")
    return render_template_string("""
        <!DOCTYPE html>
        <html lang="en" class="bg-gray-100">
        <head>
            <meta charset="UTF-8">
            <title>Doctor Login</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="flex items-center justify-center min-h-screen bg-gray-100">
            <div class="bg-white p-8 rounded-lg shadow-md w-full max-w-sm">
                <h2 class="text-2xl font-bold mb-6 text-center text-gray-800">Doctor Login</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% for category, message in messages %}
                        <div class="mb-4 text-sm p-3 rounded bg-red-100 text-red-800">
                            {{ message }}
                        </div>
                    {% endfor %}
                {% endwith %}
                <form method="POST" action="/login">
                    <div class="mb-4">
                        <label for="username" class="block text-gray-700 text-sm font-bold mb-2">Username:</label>
                        <input type="text" id="username" name="username" required
                               class="shadow appearance-none border rounded w-full py-2 px-3 text-gray-700 leading-tight focus:outline-none focus:shadow-outline">
                    </div>
                    <div class="mb-6">
                        <label for="password" class="block text-gray-700 text-sm font-bold mb-2">Password:</label>
                        <input type="password" id="password" name="password" required
                               class="shadow appearance-none border rounded w-full py-2 px-3 text-gray-700 mb-3 leading-tight focus:outline-none focus:shadow-outline">
                    </div>
                    <div class="flex items-center justify-between">
                        <button type="submit"
                                class="bg-teal-600 hover:bg-teal-700 text-white font-bold py-2 px-4 rounded focus:outline-none focus:shadow-outline">
                            Login
                        </button>
                        <a href="/" class="inline-block align-baseline font-bold text-sm text-teal-600 hover:text-teal-800">
                            Back to Home
                        </a>
                    </div>
                </form>
            </div>
        </body>
        </html>
    """) 

# In your edit_appointment route, pre-fill the phone field without the +91 prefix for editing:
@app.route("/edit_appointment/<appointment_id>", methods=["GET", "POST"])
def edit_appointment(appointment_id):
    if "doctor" not in session:
        flash("Please log in to edit appointments.", "error")
        return redirect("/login")

    appointment = appointments_collection.find_one({"appointment_id": appointment_id})
    if not appointment:
        flash("Appointment not found.", "error")
        return redirect("/dashboard")

    # Remove +91 prefix for display in the form
    phone_display = appointment.get("phone", "")
    if phone_display.startswith("+91"):
        phone_display = phone_display[3:]
    elif phone_display.startswith("91") and len(phone_display) == 12:
        phone_display = phone_display[2:]
    elif phone_display.startswith("0") and len(phone_display) == 11:
        phone_display = phone_display[1:]

    appointment["phone"] = phone_display

    # ...existing code...
    location_options = sorted({
        (b.get("location") or "").strip()
        for b in branches_collection.find({}, {"location": 1})
    })
    default_city = appointment.get("location", location_options[0] if location_options else "Hyderabad")
    # Convert appointment date to YYYY-MM-DD format for generate_time_slots
    appointment_date = appointment.get("date", "")
    if appointment_date:
        try:
            if len(appointment_date) == 10 and appointment_date[2] == '-' and appointment_date[5] == '-':
                # DD-MM-YYYY format, convert to YYYY-MM-DD
                dt = datetime.strptime(appointment_date, "%d-%m-%Y")
                appointment_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    time_slots = generate_time_slots(default_city, appointment_date)
    today_date = datetime.now().strftime("%d-%m-%Y")
    booked_slots = get_booked_slots_for_date(appointment["date"], city=default_city, exclude_appointment_id=appointment_id)

    # ...rest of your code...
    if request.method == "POST":
        try:
            name = request.form["name"]
            phone = request.form["phone"]
            email = request.form["email"]
            location = request.form.get("location", default_city)
            date_input = request.form["date"]
            time = request.form["time"]
            address = request.form["address"]
            symptoms = request.form["symptoms"]

            # Convert date to d-m-Y format for storing
            try:
                date_obj = datetime.strptime(date_input, "%Y-%m-%d")
                date = date_obj.strftime("%d-%m-%Y")
            except Exception:
                date = date_input

            normalized_phone, phone_error = normalize_indian_phone(phone)
            if phone_error:
                flash(phone_error, "error")
                return render_template_string(appointment_form_template, mode='edit', appointment_data=appointment, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

            updated_data = {
                "name": name,
                "phone": normalized_phone,
                "email": email,
                "location": location,
                "date": date,
                "time": time,
                "address": address,
                "symptoms": symptoms
            }

            # Check for slot conflicts (excluding current appointment)
            existing_appointment = appointments_collection.find_one({
                "date": date,
                "time": time,
                "location": location,
                "appointment_id": {"$ne": appointment_id}
            }) or blocked_slots_collection.find_one({
                "date": date,
                "time": time,
                "location": location
            })

            if existing_appointment:
                flash(f"The slot {date} {time} is unavailable (booked/blocked). Please choose a different time.", "error")
                return render_template_string(appointment_form_template, mode='edit', appointment_data=appointment, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

            appointments_collection.update_one({"appointment_id": appointment_id}, {"$set": updated_data})
            flash("Appointment updated successfully.", "success")
            return redirect("/dashboard")

        except Exception as e:
            flash(f"Error updating appointment: {str(e)}", "error")
            return render_template_string(appointment_form_template, mode='edit', appointment_data=appointment, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

    return render_template_string(appointment_form_template, mode='edit', appointment_data=appointment, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

# ...existing code...


@app.route("/dashboard")
def dashboard():
    if "doctor" not in session:
        flash("Please log in to access the dashboard.", "error")
        return redirect("/")
    
    search_query = request.args.get('search_query', '').strip()
    sort_by = request.args.get('sort_by', '') 
    
    query = {}
    if search_query:
        query = {
            "$or": [
                {"name": {"$regex": search_query, "$options": "i"}},
                {"appointment_id": {"$regex": search_query, "$options": "i"}},
                {"patient_name": {"$regex": search_query, "$options": "i"}} 
            ]
        }
    
    appointments = list(appointments_collection.find(query))
    
    # Show all appointments for the selected month/day without filtering out past ones
    # (Previously, past appointments were hidden which made them appear missing in the calendar.)
    
    print(f"Fetched {len(appointments)} appointments from the database for query: {query} (filtered to present and future only)")
    


    for appointment in appointments:
        # Prioritize 'created_at_str' (from Flask app insertions)
        if 'created_at_str' in appointment and appointment['created_at_str'] != 'N/A':
            # Try to parse it to ensure consistency, then re-format
            try:
                # Common format for Flask app: "DD-MM-YYYY HH:MM AM/PM IST"
                dt_obj = datetime.strptime(appointment['created_at_str'], "%d-%m-%Y %I:%M %p IST")
                appointment['created_at_str'] = dt_obj.strftime("%d-%m-%Y %I:%M %p IST")
            except ValueError:
                # If it's already a string but in a different valid format from previous runs, handle it
                # Example: "2025-07-28 09:48 PM IST"
                try:
                    dt_obj = datetime.strptime(appointment['created_at_str'], "%Y-%m-%d %I:%M %p IST") 
                    appointment['created_at_str'] = dt_obj.strftime("%d-%m-%Y %I:%M %p IST")
                except ValueError:
                    # If parsing fails, keep the original string or set to N/A
                    appointment['created_at_str'] = appointment.get('created_at_str', 'N/A')
        # Check for 'created_at' (common for manual insertions or other systems)
        elif 'created_at' in appointment:
            created_val = appointment['created_at']
            if isinstance(created_val, datetime):
                # If it's a datetime object (PyMongo default for BSON Date)
                appointment['created_at_str'] = created_val.strftime("%d-%m-%Y %I:%M %p IST")
            elif isinstance(created_val, str):
                # If it's a string, try to parse various formats
                parsed = False
                formats_to_try = [
                    "%Y-%m-%d %I:%M:%S %p", # Example: "2025-07-28 10:37:39 PM" (from your error)
                    "%Y-%m-%d %I:%M %p",    # Example: "2025-07-28 09:48 PM" (from your dashboard)
                    "%Y-%m-%d %H:%M:%S",    # Common format without AM/PM (if you have any)
                    "%d-%m-%Y %I:%M %p IST" # Already desired format (for existing correct entries)
                ]
                for fmt in formats_to_try:
                    try:
                        dt_obj = datetime.strptime(created_val, fmt)
                        appointment['created_at_str'] = dt_obj.strftime("%d-%m-%Y %I:%M %p IST")
                        parsed = True
                        break
                    except ValueError:
                        continue
                if not parsed:
                    # If all parsing attempts fail, keep original or default
                    appointment['created_at_str'] = created_val if created_val else 'N/A'
            else:
                appointment['created_at_str'] = 'N/A' # Fallback for unexpected types
        else:
            # If neither field exists, default to 'N/A'
            appointment['created_at_str'] = 'N/A'
            
        # Also ensure 'name' field is populated for display from 'patient_name' if needed
        if 'name' not in appointment and 'patient_name' in appointment:
            appointment['name'] = appointment['patient_name']

        # Ensure 'phone' field is populated from 'patient_phone' if needed
        if 'phone' not in appointment and 'patient_phone' in appointment:
            appointment['phone'] = appointment['patient_phone']
            


    # Apply sorting logic
    def get_sort_key_for_date(appointment_item):
        date_str = appointment_item.get('date', '2000-01-01')
        time_str = appointment_item.get('time', '00:00')
        
        # Normalize time_str to 24-hour format if it contains AM/PM
        if 'AM' in time_str or 'PM' in time_str:
            try:
                # Try parsing with seconds, then without seconds
                try:
                    dt_obj = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M:%S %p")
                except ValueError:
                    dt_obj = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p")
                return dt_obj
            except ValueError:
                return datetime.min # Fallback for unparseable date/time
        else:
            try:
                # Assume 24-hour format if no AM/PM
                dt_obj = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                return dt_obj
            except ValueError:
                return datetime.min # Fallback for unparseable date/time

    if sort_by == 'name_asc':
        appointments.sort(key=lambda x: x.get('name', '').lower())
    elif sort_by == 'name_desc':
        appointments.sort(key=lambda x: x.get('name', '').lower(), reverse=True)
    elif sort_by == 'date_asc':
        appointments.sort(key=get_sort_key_for_date)
    elif sort_by == 'date_desc':
        appointments.sort(key=get_sort_key_for_date, reverse=True)
    else:
        # Default sorting by created_at_str (latest first)
        def get_created_at_sort_key(appointment_item):
            created_at_str = appointment_item.get('created_at_str', '')
            if created_at_str and 'N/A' not in created_at_str:
                # Try multiple formats for created_at_str for sorting
                sort_formats_to_try = [
                    "%d-%m-%Y %I:%M %p IST",  # Your desired output format
                    "%Y-%m-%d %I:%M:%S %p",  # Format from manual entry error
                    "%Y-%m-%d %I:%M %p",     # Another possible format
                    "%Y-%m-%d %H:%M:%S",     # Another common format
                ]
                for fmt in sort_formats_to_try:
                    try:
                        return datetime.strptime(created_at_str, fmt)
                    except ValueError:
                        continue
            return datetime.min # Fallback for 'N/A' or unparseable dates
        
        appointments.sort(key=get_created_at_sort_key, reverse=True)


    # Clean up any appointments with missing or incorrect field names
    cleanup_appointments()
    
    return render_template_string(dashboard_template, doctor=session["doctor"], appointments=appointments, search_query=search_query, sort_by=sort_by)

@app.route("/cleanup_appointments")
def cleanup_appointments_route():
    if "doctor" not in session:
        flash("Please log in to access this function.", "error")
        return redirect("/")
    
    try:
        cleanup_appointments()
        flash("Appointments cleaned up successfully!", "success")
    except Exception as e:
        flash(f"Error cleaning up appointments: {str(e)}", "error")
    
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.pop("doctor", None)
    flash("You have been logged out.", "success")
    return redirect("/")

@app.route("/update_appointment_status/<appointment_id>/<status>")
def update_appointment_status(appointment_id, status):
    if "doctor" not in session:
        flash("Please log in to update appointment status.", "error")
        return redirect("/")
    
    # Expanded valid statuses based on your dashboard data
    valid_statuses = ['confirmed', 'pending', 'cancelled', 'checked_in', 'booked', 'completed']
    if status not in valid_statuses: 
        flash("Invalid status provided.", "error")
        return redirect("/dashboard")

    try:
        # Get appointment details before updating (for email notification)
        appointment = appointments_collection.find_one({"appointment_id": appointment_id})
        if not appointment:
            flash(f"Appointment with ID {appointment_id} not found.", "error")
            return redirect("/dashboard")
        
        result = appointments_collection.update_one(
            {"appointment_id": appointment_id},
            {"$set": {"status": status}}
        )
        
        if result.modified_count > 0:
            flash(f"Appointment {appointment_id} status updated to {status.capitalize()}.", "success")
            
            # Send cancellation email if status is cancelled
            if status == 'cancelled':
                email_sent = send_cancellation_email(
                    patient_name=appointment.get('name', 'Patient'),
                    patient_email=appointment.get('email', ''),
                    appointment_date=appointment.get('date', ''),
                    appointment_time=appointment.get('time', '')
                )
                if email_sent:
                    flash("Cancellation email sent to patient.", "success")
                else:
                    flash("Appointment cancelled but email notification failed.", "warning")
        else:
            flash(f"Appointment with ID {appointment_id} not found or status already {status}.", "info") 
    except Exception as e:
        flash(f"Error updating appointment: {str(e)}", "error")
    
    return redirect("/dashboard")

# ...existing code...
@app.route("/add_appointment", methods=["GET", "POST"])
def add_appointment():
    if "doctor" not in session:
        flash("Please log in to add appointments.", "error")
        return redirect("/")

    appointment_data = {}
    try:
        branch_locations = {
            (b.get("location") or "").strip()
            for b in branches_collection.find({}, {"location": 1})
        }
        branch_locations.discard("")
    except Exception:
        branch_locations = set()
    location_options = sorted(branch_locations)

    default_city = location_options[0] if location_options else 'Hyderabad'
    today_date = datetime.now().strftime("%d-%m-%Y")  # d-m-Y format for input and min

    selected_date = request.form.get("date", today_date) if request.method == "POST" else today_date
    selected_city = request.form.get("location", default_city) if request.method == "POST" else default_city
    
    # Convert selected_date to YYYY-MM-DD format for generate_time_slots
    appointment_date = selected_date
    if appointment_date:
        try:
            if len(appointment_date) == 10 and appointment_date[2] == '-' and appointment_date[5] == '-':
                # DD-MM-YYYY format, convert to YYYY-MM-DD
                dt = datetime.strptime(appointment_date, "%d-%m-%Y")
                appointment_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    
    time_slots = generate_time_slots(selected_city, appointment_date)
    booked_slots = get_booked_slots_for_date(selected_date, city=selected_city)

    if request.method == "POST":
        try:
            name = request.form["name"]
            phone = request.form["phone"]
            email = request.form["email"]
            location = request.form.get("location", default_city)
            date_input = request.form["date"]
            time = request.form["time"]
            address = request.form["address"]
            symptoms = request.form["symptoms"]
             # Convert date to d-m-Y format for storing
            try:
                date_obj = datetime.strptime(date_input, "%Y-%m-%d")
                date = date_obj.strftime("%d-%m-%Y")
            except Exception:
                date = date_input  # fallback if already in d-m-Y

            normalized_phone, phone_error = normalize_indian_phone(phone)
            if phone_error:
                flash(phone_error, "error")
                return render_template_string(appointment_form_template, mode='add', appointment_data=appointment_data, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

            appointment_data = {
                "name": name,
                "phone": normalized_phone,
                "email": email,
                "location": location,
                "date": date,  # store in d-m-Y format
                "time": time,
                "address": address,
                "symptoms": symptoms
            }

            # Compare dates in d-m-Y format
            if datetime.strptime(date, "%d-%m-%Y") < datetime.strptime(today_date, "%d-%m-%Y"):
                flash("Cannot book an appointment for a past date.", "error")
                return render_template_string(appointment_form_template, mode='add', appointment_data=appointment_data, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

            existing_appointment = appointments_collection.find_one({
                "date": date,
                "time": time,
                "location": location
            }) or blocked_slots_collection.find_one({
                "date": date,
                "time": time,
                "location": location
            })

            if existing_appointment:
                flash(f"The slot {date} {time} is unavailable (booked/blocked). Please choose a different time.", "error")
                return render_template_string(appointment_form_template, mode='add', appointment_data=appointment_data, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

            # Format location for ID (replace spaces with underscores, remove special chars)
            location_id = location.replace(" ", "_").replace(",", "").replace(".", "")
            date_str = datetime.now().strftime("%d%m%y")
            while True:
                random_num = str(random.randint(1, 9999)).zfill(4)
                potential_appointment_id = f"HeyDoc_{location_id}_{date_str}_{random_num}"
                if not appointments_collection.find_one({"appointment_id": potential_appointment_id}):
                    appointment_id = potential_appointment_id
                    break

            new_appointment_data = {
                "appointment_id": appointment_id,
                "name": name,
                "phone": normalized_phone,
                "email": email,
                "address": address,
                "symptoms": symptoms,
                "date": date,  # store in d-m-Y format
                "time": time,
                "location": location,
                "status": "pending",
                "created_at_str": datetime.now().strftime("%d-%m-%Y %I:%M %p IST")
            }

            appointments_collection.insert_one(new_appointment_data)
            flash(f"Appointment {appointment_id} created successfully.", "success")
            return redirect("/dashboard")

        except Exception as e:
            flash(f"Error creating appointment: {str(e)}", "error")
            return render_template_string(appointment_form_template, mode='add', appointment_data=appointment_data, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)

    return render_template_string(appointment_form_template, mode='add', appointment_data=appointment_data, time_slots=time_slots, today_date=today_date, booked_slots=booked_slots, location_options=location_options)
             
# ...existing code...
@app.route("/get_booked_slots/<date>")
def get_booked_slots(date):
    """API endpoint to get booked slots for a specific date. Optional query param: city."""
    if "doctor" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        city = request.args.get("city")
        booked_slots = get_booked_slots_for_date(date, city=city)
        return jsonify({"booked_slots": booked_slots})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Block/Unblock Slot Routes ---
@app.route("/block_slot", methods=["GET", "POST"])
def block_slot():
    if "doctor" not in session:
        flash("Please log in to manage slots.", "error")
        return redirect("/")

    if request.method == "POST":
        date = request.form.get("date", "").strip()
        time = request.form.get("time", "").strip()
        location = request.form.get("location", "Hyderabad").strip()
        reason = request.form.get("reason", "").strip()

        if not date or not time:
            flash("Date and Time are required.", "error")
            return redirect("/block_slot")

        # Convert date from YYYY-MM-DD to DD-MM-YYYY format for storage
        formatted_date = date
        try:
            if len(date) == 10 and date[4] == '-' and date[7] == '-':
                # YYYY-MM-DD format, convert to DD-MM-YYYY
                dt = datetime.strptime(date, "%Y-%m-%d")
                formatted_date = dt.strftime("%d-%m-%Y")
        except ValueError:
            pass

        # Prevent blocking if an appointment already exists (check both formats)
        exists = appointments_collection.find_one({"date": date, "time": time, "location": location})
        if not exists:
            # Also check with formatted date
            exists = appointments_collection.find_one({"date": formatted_date, "time": time, "location": location})
        
        if exists:
            flash(f"Cannot block {formatted_date} {time}: an appointment exists.", "error")
            return redirect("/block_slot")

        # Prevent duplicate block (check both formats)
        already_blocked = blocked_slots_collection.find_one({"date": date, "time": time, "location": location})
        if not already_blocked:
            already_blocked = blocked_slots_collection.find_one({"date": formatted_date, "time": time, "location": location})
        
        if already_blocked:
            flash("This slot is already blocked.", "info")
            return redirect("/block_slot")

        blocked_slots_collection.insert_one({
            "date": formatted_date,  # Store in DD-MM-YYYY format
            "time": time,
            "location": location,
            "reason": reason,
            "created_at": datetime.now().strftime("%d-%m-%Y %I:%M %p IST")
        })
        flash(f"Blocked {date} {time}.", "success")
        return redirect("/block_slot")

    # GET: show form and list
    all_blocked = blocked_slots_collection.find({}).sort("date", 1)
    
    # Filter out past blocked slots - only show present and future ones
    current_date = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M")
    
    blocked_list = []
    for blocked in all_blocked:
        blocked_date = blocked.get('date', '')
        blocked_time = blocked.get('time', '')
        
        # Skip if no date or time
        if not blocked_date or not blocked_time:
            continue
        
        # Normalize blocked date to YYYY-MM-DD for comparison
        normalized_date = blocked_date
        try:
            if len(blocked_date) == 10 and blocked_date[2] == '-' and blocked_date[5] == '-':
                # DD-MM-YYYY format, convert to YYYY-MM-DD for comparison
                dt = datetime.strptime(blocked_date, "%d-%m-%Y")
                normalized_date = dt.strftime("%Y-%m-%d")
            elif len(blocked_date) == 10 and blocked_date[4] == '-' and blocked_date[7] == '-':
                # Already YYYY-MM-DD format
                normalized_date = blocked_date
        except ValueError:
            # If date parsing fails, skip this blocked slot
            continue
            
        # Convert blocked time to 24-hour format for comparison
        try:
            if 'AM' in blocked_time or 'PM' in blocked_time:
                # Parse 12-hour format
                try:
                    time_obj = datetime.strptime(blocked_time, "%I:%M %p")
                except ValueError:
                    time_obj = datetime.strptime(blocked_time, "%I:%M:%S %p")
                blocked_time_24 = time_obj.strftime("%H:%M")
            else:
                # Already in 24-hour format
                blocked_time_24 = blocked_time
        except ValueError:
            # If time parsing fails, skip this blocked slot
            continue
        
        # Check if blocked slot is today or in the future
        if normalized_date > current_date:
            # Future date - include
            # Ensure date is in DD-MM-YYYY format for display
            if len(blocked_date) == 10 and blocked_date[4] == '-' and blocked_date[7] == '-':
                # YYYY-MM-DD format, convert to DD-MM-YYYY
                try:
                    dt = datetime.strptime(blocked_date, "%Y-%m-%d")
                    blocked['date'] = dt.strftime("%d-%m-%Y")
                except ValueError:
                    pass
            blocked_list.append(blocked)
        elif normalized_date == current_date:
            # Today - only include if time is current or future
            if blocked_time_24 >= current_time:
                # Ensure date is in DD-MM-YYYY format for display
                if len(blocked_date) == 10 and blocked_date[4] == '-' and blocked_date[7] == '-':
                    # YYYY-MM-DD format, convert to DD-MM-YYYY
                    try:
                        dt = datetime.strptime(blocked_date, "%Y-%m-%d")
                        blocked['date'] = dt.strftime("%d-%m-%Y")
                    except ValueError:
                        pass
                blocked_list.append(blocked)
        # Past dates are automatically excluded
    # Get selected date for time slot generation (default to today)
    selected_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    
    return render_template_string(
        block_slot_template,
        time_slots=generate_time_slots("Hyderabad", selected_date),
        blocked_list=blocked_list,
        datetime=datetime,
        available_cities=AVAILABLE_CITIES
    )

@app.route("/unblock_slot")
def unblock_slot():
    if "doctor" not in session:
        flash("Please log in to manage slots.", "error")
        return redirect("/")

    sid = request.args.get("id", "").strip()
    try:
        if sid:
            blocked_slots_collection.delete_one({"_id": ObjectId(sid)})
            flash("Slot unblocked.", "success")
    except Exception as e:
        flash(f"Error unblocking slot: {e}", "error")
    return redirect("/block_slot")

# Migration function to update existing blocked slots to DD-MM-YYYY format
@app.route("/migrate_blocked_slots")
def migrate_blocked_slots():
    if "doctor" not in session:
        flash("Please log in to access this function.", "error")
        return redirect("/")
    
    try:
        # Find all blocked slots with YYYY-MM-DD format
        all_blocked = blocked_slots_collection.find({})
        updated_count = 0
        
        for blocked in all_blocked:
            blocked_date = blocked.get('date', '')
            
            # Check if it's in YYYY-MM-DD format
            if len(blocked_date) == 10 and blocked_date[4] == '-' and blocked_date[7] == '-':
                try:
                    # Convert to DD-MM-YYYY format
                    dt = datetime.strptime(blocked_date, "%Y-%m-%d")
                    new_date = dt.strftime("%d-%m-%Y")
                    
                    # Update the document
                    blocked_slots_collection.update_one(
                        {"_id": blocked["_id"]},
                        {"$set": {"date": new_date}}
                    )
                    updated_count += 1
                except ValueError:
                    # Skip if date parsing fails
                    continue
        
        flash(f"Migration completed. Updated {updated_count} blocked slots to DD-MM-YYYY format.", "success")
    except Exception as e:
        flash(f"Error during migration: {str(e)}", "error")
    
    return redirect("/block_slot")


# --- Public API: get generated time slots for a city ---
@app.route("/get_time_slots")
def api_get_time_slots():
    if "doctor" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    city = request.args.get("city", "Hyderabad")
    # Optional date (YYYY-MM-DD) to allow date-specific overrides from Mongo
    for_date = request.args.get("date")
    try:
        slots = generate_time_slots(city, for_date)
        return jsonify({"time_slots": slots, "city": city, "date": for_date})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Availability Routes ---
# ...existing code...

@app.route("/add_availability", methods=["GET", "POST"])
def add_availability():
    if "doctor" not in session:
        flash("Please log in to manage availability.", "error")
        return redirect("/")

    # Build location options: branches only (no default cities)
    try:
        branch_locations = {
            (b.get("location") or "").strip()
            for b in branches_collection.find({}, {"location": 1})
        }
        branch_locations.discard("")
    except Exception:
        branch_locations = set()
    location_options = sorted(branch_locations)  # <-- Only branch locations

    if request.method == "POST":
        try:
            location = request.form.get("location", "").strip()
            def is_real_place(loc_name: str) -> bool:
                try:
                    if not loc_name:
                        return False
                    url = "https://nominatim.openstreetmap.org/search"
                    params = {"q": loc_name, "format": "json", "addressdetails": 1, "limit": 1}
                    headers = {"User-Agent": "clinic-app/1.0"}
                    r = requests.get(url, params=params, headers=headers, timeout=6)
                    if r.status_code != 200:
                        return False
                    data = r.json()
                    return isinstance(data, list) and len(data) > 0
                except Exception:
                    return False

            if location_options:
                if location not in location_options:
                    flash("Please select a location from Branch list.", "error")
                    return render_template_string(availability_form_template, datetime=datetime, location_options=location_options)
            else:
                if not is_real_place(location):
                    flash("Please enter a real location name (validated against maps).", "error")
                    return render_template_string(availability_form_template, datetime=datetime, location_options=location_options)
            hospital_name = request.form.get("hospital_name", "Hey Doc!").strip()
            mode = request.form.get("mode", "default")
            date_override = request.form.get("date", "").strip()

            def fmt_12h(value):
                if not value:
                    return None
                try:
                    t = datetime.strptime(value, "%H:%M")
                    return t.strftime("%I:%M %p")
                except Exception:
                    return value

            morning_start = fmt_12h(request.form.get("morning_start"))
            morning_end = fmt_12h(request.form.get("morning_end"))
            evening_start = fmt_12h(request.form.get("evening_start"))
            evening_end = fmt_12h(request.form.get("evening_end"))

            working_hours = {}
            if morning_start and morning_end:
                working_hours["morning_shift"] = {"start": morning_start, "end": morning_end}
            if evening_start and evening_end:
                working_hours["evening_shift"] = {"start": evening_start, "end": evening_end}

            if not working_hours:
                flash("Please enter at least one complete shift (start and end).", "error")
                return render_template_string(availability_form_template, datetime=datetime, location_options=location_options)

            timings_col = loc_aval_collection

            doc = {
                "hospital_name": hospital_name or "Hey Doc ",
                "location": location,
                "working_hours": working_hours,
                "created_at": datetime.utcnow()
            }

            if mode == "date" and date_override:
                try:
                    dt = datetime.strptime(date_override, "%Y-%m-%d")
                    doc["date"] = dt.strftime("%d-%m-%Y")
                except Exception:
                    doc["date"] = date_override
                doc["Default"] = False
            else:
                doc["Default"] = True

            timings_col.insert_one(doc)
            flash("Availability saved.", "success")
            return redirect("/dashboard")
        except Exception as e:
            flash(f"Error saving availability: {e}", "error")
            return render_template_string(availability_form_template, datetime=datetime, location_options=location_options)

    return render_template_string(availability_form_template, datetime=datetime, location_options=location_options)

# ...existing code...

# --- Branch Management Routes ---
@app.route("/add_branch", methods=["GET", "POST"])
def add_branch():
    if "doctor" not in session:
        flash("Please log in to manage branches.", "error")
        return redirect("/")

    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            location = request.form.get("location", "").strip()
            address = request.form.get("address", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            notes = request.form.get("notes", "").strip()
            morning_start = request.form.get("morning_start", "").strip()
            morning_end = request.form.get("morning_end", "").strip()
            evening_start = request.form.get("evening_start", "").strip()
            evening_end = request.form.get("evening_end", "").strip()
            is_default = request.form.get("is_default") == "on"

            if not name:
                flash("Branch name is required.", "error")
                return redirect("/add_branch")

            doc = {
                "name": name,
                "location": location,
                "address": address,
                "phone": phone,
                "email": email,
                "notes": notes,
                "created_at": datetime.utcnow(),
                "created_by": session.get("doctor")
            }

            branches_collection.insert_one(doc)
            # Also store timings in LocAval as requested
            def _fmt_12h(value):
                try:
                    # Accept both 12h (with AM/PM) and 24h, return 12h with AM/PM
                    return datetime.strptime(value, "%I:%M %p").strftime("%I:%M %p")
                except Exception:
                    try:
                        return datetime.strptime(value, "%H:%M").strftime("%I:%M %p")
                    except Exception:
                        return value or None

            working_hours = {}
            if morning_start and morning_end:
                working_hours["morning_shift"] = {"start": _fmt_12h(morning_start), "end": _fmt_12h(morning_end)}
            if evening_start and evening_end:
                working_hours["evening_shift"] = {"start": _fmt_12h(evening_start), "end": _fmt_12h(evening_end)}

            if working_hours:
                locaval_doc = {
                    "hospital_name": name,
                    "location": location,
                    "Default": True if is_default else False,
                    "working_hours": working_hours,
                    "created_at": datetime.utcnow()
                }
                loc_aval_collection.insert_one(locaval_doc)

            flash("Branch added successfully.", "success")
            return redirect("/dashboard")
        except Exception as e:
            flash(f"Error adding branch: {e}", "error")

    # GET
    return render_template_string(
        """
        <!DOCTYPE html>
        <html lang=\"en\" class=\"bg-gray-100\">
        <head>
          <meta charset=\"UTF-8\">
          <title>Add Branch - Hey Doc!</title>
          <script src=\"https://cdn.tailwindcss.com\"></script>
        </head>
        <body class=\"min-h-screen bg-gray-100\">
          <nav class=\"bg-teal-600 p-4 text-white flex justify-between items-center\">
            <h1 class=\"text-xl font-bold\">Add Branch</h1>
            <div>
              <a href=\"/dashboard\" class=\"bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100\">Dashboard</a>
            </div>
          </nav>
          <div class=\"p-6 max-w-2xl mx-auto\">
            {% with messages = get_flashed_messages(with_categories=true) %}
              {% for category, message in messages %}
                <div class=\"mb-4 text-sm p-3 rounded bg-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-100 text-{{ 'red' if category == 'error' else 'green' if category == 'success' else 'blue' }}-800\">{{ message }}</div>
              {% endfor %}
            {% endwith %}

            <div class=\"bg-white rounded-lg shadow-md p-6\">
              <form method=\"POST\" action=\"/add_branch\" class=\"space-y-4\">
                <div>
                  <label class=\"block text-gray-700 mb-1\">Branch Name<span class=\"text-red-500\">*</span></label>
                  <input type=\"text\" name=\"name\" required class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"e.g., Hey Doc Clinic - Hyderabad\" />
                </div>
                <div>
                  <label class=\"block text-gray-700 mb-1\">Location / City</label>
                  <input type=\"text\" name=\"location\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"e.g., Hyderabad\" />
                </div>
                <div>
                  <label class=\"block text-gray-700 mb-1\">Address</label>
                  <textarea name=\"address\" rows=\"3\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"Street, Area, Pin\"></textarea>
                </div>
                <div class=\"grid grid-cols-1 md:grid-cols-2 gap-4\">
                  <div>
                    <label class=\"block text-gray-700 mb-1\">Phone</label>
                    <input type=\"text\" name=\"phone\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"e.g., +91XXXXXXXXXX\" />
                  </div>
                  <div>
                    <label class=\"block text-gray-700 mb-1\">Email</label>
                    <input type=\"email\" name=\"email\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"e.g., branch@example.com\" />
                  </div>
                </div>
                <div class=\"grid grid-cols-1 md:grid-cols-2 gap-4\">
                  <div>
                    <label class=\"block text-gray-700 font-medium mb-2\">Morning Shift</label>
                    <div class=\"grid grid-cols-2 gap-2\">
                      <input type=\"text\" name=\"morning_start\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"11:00 AM\" />
                      <input type=\"text\" name=\"morning_end\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"02:00 PM\" />
                    </div>
                  </div>
                  <div>
                    <label class=\"block text-gray-700 font-medium mb-2\">Evening Shift</label>
                    <div class=\"grid grid-cols-2 gap-2\">
                      <input type=\"text\" name=\"evening_start\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"06:00 PM\" />
                      <input type=\"text\" name=\"evening_end\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"09:30 PM\" />
                    </div>
                  </div>
                </div>
                <div class=\"flex items-center space-x-2\">
                  <input id=\"is_default\" type=\"checkbox\" name=\"is_default\" class=\"h-4 w-4\">
                  <label for=\"is_default\" class=\"text-gray-700\">Mark as Default timings for this location</label>
                </div>
                <div>
                  <label class=\"block text-gray-700 mb-1\">Notes</label>
                  <textarea name=\"notes\" rows=\"2\" class=\"w-full px-4 py-2 border border-gray-300 rounded focus:outline-none focus:border-teal-500\" placeholder=\"Any additional details\"></textarea>
                </div>

                <div class=\"flex items-center space-x-3\">
                  <button type=\"submit\" class=\"bg-teal-600 text-white px-5 py-2 rounded hover:bg-teal-700\">Save Branch</button>
                  <a href=\"/dashboard\" class=\"bg-gray-200 text-gray-700 px-5 py-2 rounded hover:bg-gray-300\">Cancel</a>
                </div>
              </form>
            </div>
          </div>
        </body>
        </html>
        """,
    )

# --- Prescription Routes ---
@app.route("/add_prescription", methods=["GET", "POST"])
def add_prescription():
    if "doctor" not in session:
        flash("Please log in to add prescriptions.", "error")
        return redirect("/")
    
    prescription_data = {}
    today_date = datetime.now().strftime("%Y-%m-%d")
    
                # Check for patient information from query parameters (when coming from patient-specific view)
    if request.method == "GET":
        patient_phone = request.args.get('patient_phone', '').strip()
        print(f"DEBUG: Received patient_phone parameter: '{patient_phone}'")
        if patient_phone:
            # Normalize phone number for search (remove +91 if present, add if missing)
            normalized_phone = patient_phone
            if patient_phone.startswith('+91'):
                normalized_phone = patient_phone[3:]  # Remove +91
            elif patient_phone.startswith('91'):
                normalized_phone = patient_phone[2:]  # Remove 91
            elif patient_phone.startswith('0'):
                normalized_phone = patient_phone[1:]  # Remove leading 0
            
            # Try multiple phone number formats for search
            phone_variants = [
                patient_phone,  # Original format
                f"+91{normalized_phone}",  # With +91
                f"91{normalized_phone}",   # With 91
                f"0{normalized_phone}",    # With 0
                normalized_phone           # Clean number
            ]
            
            print(f"DEBUG: Searching with phone variants: {phone_variants}")
            
            # Try to get patient name from appointments
            appointment = None
            for phone_variant in phone_variants:
                appointment = appointments_collection.find_one({"phone": phone_variant})
                if appointment:
                    print(f"DEBUG: Found appointment with phone variant: '{phone_variant}'")
                    break
            
            if appointment:
                prescription_data["patient_name"] = appointment.get("name", "")
                prescription_data["patient_phone"] = appointment.get("phone", patient_phone)
                print(f"DEBUG: Found appointment for {patient_phone}, name: {appointment.get('name', '')}")
            else:
                # Check if patient exists in prescriptions
                prescription = None
                for phone_variant in phone_variants:
                    prescription = prescriptions_collection.find_one({"patient_phone": phone_variant})
                    if prescription:
                        print(f"DEBUG: Found prescription with phone variant: '{phone_variant}'")
                        break
                
                if prescription:
                    prescription_data["patient_name"] = prescription.get("patient_name", "")
                    prescription_data["patient_phone"] = prescription.get("patient_phone", patient_phone)
                    print(f"DEBUG: Found prescription for {patient_phone}, name: {prescription.get('patient_name', '')}")
                else:
                    print(f"DEBUG: No patient found for phone: {patient_phone}")
                    # Let's also check what phone numbers exist in the database
                    all_appointments = list(appointments_collection.find({}, {"phone": 1, "name": 1}))
                    print(f"DEBUG: All phone numbers in appointments: {[a.get('phone') for a in all_appointments]}")
                    all_prescriptions = list(prescriptions_collection.find({}, {"patient_phone": 1, "patient_name": 1}))
                    print(f"DEBUG: All phone numbers in prescriptions: {[p.get('patient_phone') for p in all_prescriptions]}")
        
        print(f"DEBUG: Final prescription_data: {prescription_data}")
    
    if request.method == "POST":
        try:
            patient_name = request.form["patient_name"]
            patient_phone = request.form["patient_phone"]
            prescription_date = request.form["prescription_date"]
            # Convert input (YYYY-MM-DD) to IST display format (DD-MM-YYYY)
            try:
                _pd = datetime.strptime(prescription_date, "%Y-%m-%d")
                prescription_date_ist = _pd.strftime("%d-%m-%Y")
            except Exception:
                prescription_date_ist = prescription_date
            diagnosis = request.form["diagnosis"]
            instructions = request.form["instructions"]
            notes = request.form["notes"]
            
            # Normalize phone number to ensure +91 prefix
            normalized_phone, phone_error = normalize_indian_phone(patient_phone)
            if phone_error:
                flash(phone_error, "error")
                prescription_data = {
                    "patient_name": patient_name,
                    "patient_phone": patient_phone,
                    "prescription_date": prescription_date,
                    "diagnosis": diagnosis,
                    "instructions": instructions,
                    "notes": notes
                }
                return render_template_string(prescription_form_template, prescription_data=prescription_data, today_date=today_date)
            
            # Get medicine data from form arrays
            medicine_names = request.form.getlist("medicine_names[]")
            potencies = request.form.getlist("potencies[]")
            dosages = request.form.getlist("dosages[]")
            durations = request.form.getlist("durations[]")
            
            # Validate that we have at least one medicine
            if not medicine_names or not medicine_names[0]:
                flash("At least one medicine is required.", "error")
                prescription_data = {
                    "patient_name": patient_name,
                    "patient_phone": normalized_phone,
                    "prescription_date": prescription_date,
                    "diagnosis": diagnosis,
                    "instructions": instructions,
                    "notes": notes
                }
                return render_template_string(prescription_form_template, prescription_data=prescription_data, today_date=today_date)
            
            # Create medicines list
            medicines = []
            for i in range(len(medicine_names)):
                if medicine_names[i].strip():  # Only add if medicine name is not empty
                    medicines.append({
                        "name": medicine_names[i].strip(),
                        "potency": potencies[i].strip() if i < len(potencies) else "",
                        "dosage": dosages[i].strip() if i < len(dosages) else "",
                        "duration": durations[i].strip() if i < len(durations) else ""
                    })
            
            # Generate prescription ID
            date_str = datetime.now().strftime("%Y%m%d")
            while True:
                random_num = str(random.randint(1, 9999)).zfill(4)
                potential_prescription_id = f"PRES-{date_str}-{random_num}"
                if not prescriptions_collection.find_one({"prescription_id": potential_prescription_id}):
                    prescription_id = potential_prescription_id
                    break
            
            new_prescription_data = {
                "prescription_id": prescription_id,
                "patient_name": patient_name,
                "patient_phone": normalized_phone,
                # Store display date in IST style, and keep original ISO for queries/sorting
                "prescription_date": prescription_date_ist,
                "prescription_date_iso": prescription_date,
                "diagnosis": diagnosis,
                "medicines": medicines,
                "instructions": instructions,
                "notes": notes,
                "created_at_str": datetime.now().strftime("%d-%m-%Y %I:%M %p IST")
            }
            
            prescriptions_collection.insert_one(new_prescription_data)
            flash(f"Prescription {prescription_id} created successfully.", "success")
            
            # Redirect back to patient-specific view if we came from there
            if normalized_phone:
                return redirect(f"/prescriptions?patient_phone={normalized_phone}")
            else:
                return redirect("/prescriptions")
            
        except Exception as e:
            flash(f"Error creating prescription: {str(e)}", "error")
            prescription_data = {
                "patient_name": patient_name if 'patient_name' in locals() else "",
                "patient_phone": normalized_phone if 'normalized_phone' in locals() else (patient_phone if 'patient_phone' in locals() else ""),
                "prescription_date": prescription_date if 'prescription_date' in locals() else today_date,
                "diagnosis": diagnosis if 'diagnosis' in locals() else "",
                "instructions": instructions if 'instructions' in locals() else "",
                "notes": notes if 'notes' in locals() else ""
            }
            return render_template_string(prescription_form_template, prescription_data=prescription_data, today_date=today_date)
    
    print(f"DEBUG: Final render with prescription_data: {prescription_data}")
    print(f"DEBUG: Template will receive prescription_data.patient_name: '{prescription_data.get('patient_name', 'NOT_FOUND')}'")
    print(f"DEBUG: Template will receive prescription_data.patient_phone: '{prescription_data.get('patient_phone', 'NOT_FOUND')}'")
    return render_template_string(prescription_form_template, prescription_data=prescription_data, today_date=today_date)

@app.route("/prescriptions")
def prescriptions():
    if "doctor" not in session:
        flash("Please log in to view prescriptions.", "error")
        return redirect("/")
    
    search_query = request.args.get('search_query', '').strip()
    sort_by = request.args.get('sort_by', '')
    patient_phone = request.args.get('patient_phone', '').strip()
    
    query = {}
    if patient_phone:
        # Filter by specific patient phone number
        query["patient_phone"] = patient_phone
    elif search_query:
        query = {
            "$or": [
                {"patient_name": {"$regex": search_query, "$options": "i"}},
                {"patient_phone": {"$regex": search_query, "$options": "i"}},
                {"prescription_id": {"$regex": search_query, "$options": "i"}}
            ]
        }
    
    prescriptions_list = list(prescriptions_collection.find(query))
    
    # Apply sorting
    if sort_by == 'patient_name_asc':
        prescriptions_list.sort(key=lambda x: x.get('patient_name', '').lower())
    elif sort_by == 'patient_name_desc':
        prescriptions_list.sort(key=lambda x: x.get('patient_name', '').lower(), reverse=True)
    elif sort_by == 'date_asc':
        prescriptions_list.sort(key=lambda x: x.get('prescription_date_iso', x.get('prescription_date', '')))
    elif sort_by == 'date_desc':
        prescriptions_list.sort(key=lambda x: x.get('prescription_date_iso', x.get('prescription_date', '')), reverse=True)
    else:
        # Default sorting by created_at_str (latest first)
        def get_created_at_sort_key(prescription_item):
            created_at_str = prescription_item.get('created_at_str', '')
            if created_at_str and 'N/A' not in created_at_str:
                try:
                    return datetime.strptime(created_at_str, "%d-%m-%Y %I:%M %p IST")
                except ValueError:
                    return datetime.min
            return datetime.min
        
        prescriptions_list.sort(key=get_created_at_sort_key, reverse=True)
    
    # Get patient name for display if filtering by patient_phone
    patient_name = ""
    if patient_phone and prescriptions_list:
        # Get the patient name from the first prescription
        patient_name = prescriptions_list[0].get('patient_name', '')
    elif patient_phone:
        # If no prescriptions found, try to get patient name from appointments
        appointment = appointments_collection.find_one({"phone": patient_phone})
        if appointment:
            patient_name = appointment.get('name', '')
    
    return render_template_string(prescription_history_template, prescriptions=prescriptions_list, search_query=search_query, sort_by=sort_by, patient_phone=patient_phone, patient_name=patient_name)

@app.route("/view_prescription/<prescription_id>")
def view_prescription(prescription_id):
    if "doctor" not in session:
        flash("Please log in to view prescriptions.", "error")
        return redirect("/")
    
    prescription = prescriptions_collection.find_one({"prescription_id": prescription_id})
    
    if not prescription:
        flash("Prescription not found.", "error")
        return redirect("/prescriptions")
    
    # Get patient_phone from query parameter for back navigation
    patient_phone = request.args.get('patient_phone', '')
    
    # Create a detailed view template for single prescription
    detailed_template = """
    <!DOCTYPE html>
    <html lang="en" class="bg-gray-100">
    <head>
      <meta charset="UTF-8">
      <title>Prescription Details - Hey Doc!</title>
      <script src="https://cdn.tailwindcss.com"></script>
      <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.6.0/remixicon.min.css">
    </head>
    <body>
      <nav class="bg-teal-600 p-4 text-white flex justify-between items-center">
        <h1 class="text-xl font-bold">Hey Doc! - Prescription Details</h1>
        <div>
          <a href="/prescriptions{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">Back to Prescriptions</a>
          <a href="/dashboard" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100 mr-2">Dashboard</a>
          <a href="{{ url_for('logout') }}" class="bg-white text-teal-700 px-3 py-1 rounded hover:bg-teal-100">Logout</a>
        </div>
      </nav>

      <div class="p-6">
        <div class="bg-white rounded-lg shadow-md p-8 max-w-4xl mx-auto">
          <div class="flex justify-between items-start mb-6">
            <div>
              <h2 class="text-3xl font-bold text-gray-800">{{ prescription.patient_name }}</h2>
              <p class="text-lg text-gray-600">{{ prescription.patient_phone }}</p>
              <p class="text-gray-500">Prescription ID: {{ prescription.prescription_id }}</p>
            </div>
            <div class="text-right">
              <p class="text-sm text-gray-500">Prescription Date</p>
              <p class="text-lg font-semibold text-gray-800">{{ prescription.prescription_date }}</p>
              <p class="text-sm text-gray-500">{{ prescription.created_at_str }}</p>
            </div>
          </div>
          
          <div class="grid md:grid-cols-2 gap-8 mb-8">
            <div>
              <h3 class="text-xl font-semibold text-gray-700 mb-3">Diagnosis</h3>
              <p class="text-gray-600 text-lg">{{ prescription.diagnosis }}</p>
            </div>
            <div>
              <h3 class="text-xl font-semibold text-gray-700 mb-3">Special Instructions</h3>
              <p class="text-gray-600">{{ prescription.instructions or 'None provided' }}</p>
            </div>
          </div>
          
          <div class="mb-8">
            <h3 class="text-xl font-semibold text-gray-700 mb-4">Medicines Prescribed</h3>
            <div class="bg-gray-50 rounded-lg p-6">
              {% for medicine in prescription.medicines %}
              <div class="border border-gray-200 rounded-lg p-4 mb-4 last:mb-0">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <h4 class="font-semibold text-gray-800 text-lg mb-2">{{ medicine.name }}</h4>
                    <div class="space-y-2">
                      <div class="flex justify-between">
                        <span class="font-medium text-gray-700">Potency:</span>
                        <span class="text-gray-600">{{ medicine.potency }}</span>
                      </div>
                      <div class="flex justify-between">
                        <span class="font-medium text-gray-700">Dosage:</span>
                        <span class="text-gray-600">{{ medicine.dosage }}</span>
                      </div>
                      <div class="flex justify-between">
                        <span class="font-medium text-gray-700">Duration:</span>
                        <span class="text-gray-600">{{ medicine.duration }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              {% endfor %}
            </div>
          </div>
          
          {% if prescription.notes %}
          <div class="mb-8">
            <h3 class="text-xl font-semibold text-gray-700 mb-3">Doctor's Notes</h3>
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4">
              <p class="text-gray-700">{{ prescription.notes }}</p>
            </div>
          </div>
          {% endif %}
          
          <div class="flex justify-center space-x-4 pt-6 border-t border-gray-200">
            <a href="/prescriptions" class="bg-gray-500 text-white px-6 py-3 rounded-lg hover:bg-gray-600 transition-colors">
              <i class="ri-arrow-left-line mr-2"></i>Back to Prescriptions
            </a>
            <a href="/print_prescription/{{ prescription.prescription_id }}" class="bg-green-500 text-white px-6 py-3 rounded-lg hover:bg-green-600 transition-colors">
              <i class="ri-printer-line mr-2"></i>Print Prescription
            </a>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    
    return render_template_string(detailed_template, prescription=prescription, patient_phone=patient_phone)

@app.route("/print_prescription/<prescription_id>")
def print_prescription(prescription_id):
    if "doctor" not in session:
        flash("Please log in to print prescriptions.", "error")
        return redirect("/")
    
    prescription = prescriptions_collection.find_one({"prescription_id": prescription_id})
    
    if not prescription:
        flash("Prescription not found.", "error")
        return redirect("/prescriptions")
    
    # Get patient_phone from query parameter for back navigation
    patient_phone = request.args.get('patient_phone', '')
    
    # Create a print-friendly template
    print_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <title>Prescription - {{ prescription.patient_name }}</title>
      <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; }
        .header { text-align: center; border-bottom: 2px solid #333; padding-bottom: 20px; margin-bottom: 30px; }
        .clinic-name { font-size: 24px; font-weight: bold; margin-bottom: 5px; }
        .clinic-info { font-size: 14px; color: #666; }
        .patient-info { margin-bottom: 30px; }
        .patient-info h3 { margin: 0 0 10px 0; color: #333; }
        .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 30px; }
        .section { margin-bottom: 25px; }
        .section h4 { margin: 0 0 10px 0; color: #333; border-bottom: 1px solid #ccc; padding-bottom: 5px; }
        .medicine { border: 1px solid #ddd; padding: 15px; margin-bottom: 15px; border-radius: 5px; }
        .medicine h5 { margin: 0 0 10px 0; color: #333; }
        .medicine-details { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; }
        .detail-item { margin-bottom: 8px; }
        .detail-label { font-weight: bold; color: #555; }
        .footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #ccc; }
        .signature-line { margin-top: 50px; }
        @media print {
          body { margin: 0; }
          .no-print { display: none; }
        }
      </style>
    </head>
    <body>
      <div class="header">
        <div class="clinic-name">Hey Doc!</div>
        <div class="clinic-info">Dr. Priya Sharma, BHMS, MD (Homeopathy)</div>
        <div class="clinic-info">Hyderabad, India | Phone: +91 98765 43210</div>
      </div>
      
      <div class="patient-info">
        <h3>Patient Information</h3>
        <div class="info-grid">
          <div><strong>Name:</strong> {{ prescription.patient_name }}</div>
          <div><strong>Phone:</strong> {{ prescription.patient_phone }}</div>
          <div><strong>Prescription Date:</strong> {{ prescription.prescription_date }}</div>
          <div><strong>Prescription ID:</strong> {{ prescription.prescription_id }}</div>
        </div>
      </div>
      
      <div class="section">
        <h4>Diagnosis</h4>
        <p>{{ prescription.diagnosis }}</p>
      </div>
      
      <div class="section">
        <h4>Medicines Prescribed</h4>
        {% for medicine in prescription.medicines %}
        <div class="medicine">
          <h5>{{ medicine.name }}</h5>
          <div class="medicine-details">
            <div class="detail-item">
              <span class="detail-label">Potency:</span> {{ medicine.potency }}
            </div>
            <div class="detail-item">
              <span class="detail-label">Dosage:</span> {{ medicine.dosage }}
            </div>
            <div class="detail-item">
              <span class="detail-label">Duration:</span> {{ medicine.duration }}
            </div>
          </div>
        </div>
        {% endfor %}
      </div>
      
      {% if prescription.instructions %}
      <div class="section">
        <h4>Special Instructions</h4>
        <p>{{ prescription.instructions }}</p>
      </div>
      {% endif %}
      
      {% if prescription.notes %}
      <div class="section">
        <h4>Doctor's Notes</h4>
        <p>{{ prescription.notes }}</p>
      </div>
      {% endif %}
      
      <div class="footer">
        <div class="signature-line">
          <p>_________________________</p>
          <p><strong>Dr. Priya Sharma</strong></p>
          <p>BHMS, MD (Homeopathy)</p>
          <p>Date: {{ prescription.prescription_date }}</p>
        </div>
      </div>
      
      <div class="no-print" style="text-align: center; margin-top: 30px;">
        <button onclick="window.print()" style="background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; margin-right: 10px;">Print</button>
        <a href="/prescriptions{% if patient_phone %}?patient_phone={{ patient_phone }}{% endif %}" style="background: #666; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Back to Prescriptions</a>
      </div>
    </body>
    </html>
    """
    
    return render_template_string(print_template, prescription=prescription, patient_phone=patient_phone)

@app.route("/delete_prescription/<prescription_id>")
def delete_prescription(prescription_id):
    if "doctor" not in session:
        flash("Please log in to delete prescriptions.", "error")
        return redirect("/")
    
    prescription = prescriptions_collection.find_one({"prescription_id": prescription_id})
    
    if not prescription:
        flash("Prescription not found.", "error")
        return redirect("/prescriptions")
    
    try:
        prescriptions_collection.delete_one({"prescription_id": prescription_id})
        flash(f"Prescription {prescription_id} deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting prescription: {str(e)}", "error")
    
    # Redirect back to prescriptions page, preserving patient_phone if it was a patient-specific view
    patient_phone = request.args.get('patient_phone', '')
    if patient_phone:
        return redirect(f"/prescriptions?patient_phone={patient_phone}")
    else:
        return redirect("/prescriptions")

# Calendar View Route
@app.route("/calendar")
def calendar_view():
    if "doctor" not in session:
        flash("Please log in to view calendar.", "error")
        return redirect("/")
    
    # Get month and year from query parameters, default to current month
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    day = request.args.get('day', None, type=int)  # New: specific day filter
    
    # Get all appointments, normalize their dates and filter for the selected month/day
    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1)
    else:
        end_date = datetime(year, month + 1, 1)

    all_appts = list(appointments_collection.find({}))
    appointments = []
    for appt in all_appts:
        raw_date = appt.get("date", "")
        parsed_dt = None
        # Try both common formats used across the app and DB
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                parsed_dt = datetime.strptime(raw_date, fmt)
                break
            except Exception:
                continue
        if not parsed_dt:
            continue
        if parsed_dt.year == year and parsed_dt.month == month and (day is None or parsed_dt.day == day):
            appt["_normalized_date"] = parsed_dt.strftime("%Y-%m-%d")
            appointments.append(appt)
    
    # Do not filter out past appointments; show all for the selected month/day
    
    print(f"Raw appointments found: {len(appointments)} (filtered to present and future only)")
    for app in appointments:
        print(f"  Appointment: {app.get('appointment_id')} - {app.get('name')} - {app.get('date')} - {app.get('time')}")
    
    # Organize appointments by normalized YYYY-MM-DD date keys for correct calendar placement
    appointments_by_date = {}
    for appointment in appointments:
        date_key = appointment.get('_normalized_date') or appointment.get('date')
        if date_key not in appointments_by_date:
            appointments_by_date[date_key] = []
        appointments_by_date[date_key].append(appointment)
    
    print(f"Appointments by date: {appointments_by_date}")
    
    # Generate calendar data
    calendar_data = generate_calendar_data(year, month, appointments_by_date)
    
    # Generate filter options
    current_year = datetime.now().year
    years = list(range(current_year - 2, current_year + 3))  # 2 years back, current, 2 years forward
    months = [
        (1, "January"), (2, "February"), (3, "March"), (4, "April"),
        (5, "May"), (6, "June"), (7, "July"), (8, "August"),
        (9, "September"), (10, "October"), (11, "November"), (12, "December")
    ]
    
    print(f"Calendar view - Year: {year}, Month: {month}, Day: {day}")
    print(f"Found {len(appointments)} appointments")
    
    return render_template_string(calendar_template, 
                                calendar_data=calendar_data, 
                                year=year, 
                                month=month, 
                                day=day,
                                month_name=start_date.strftime("%B"),
                                doctor=session["doctor"],
                                years=years,
                                months=months,
                                current_year=current_year)

def generate_calendar_data(year, month, appointments_by_date):
    """Generate calendar data for the specified month"""
    # Get the first day of the month and the number of days
    first_day = datetime(year, month, 1)
    if month == 12:
        last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1) - timedelta(days=1)
    
    # Get the day of week for the first day (0 = Monday, 6 = Sunday)
    first_day_weekday = first_day.weekday()
    
    # Calculate the number of days in the month
    days_in_month = last_day.day
    
    # Generate calendar grid
    calendar_weeks = []
    current_week = []
    
    # Add empty cells for days before the first day of the month
    for _ in range(first_day_weekday):
        current_week.append({"day": None, "appointments": []})
    
    # Add days of the month
    for day in range(1, days_in_month + 1):
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        appointments = appointments_by_date.get(date_str, [])
        
        if appointments:
            print(f"Day {day} has {len(appointments)} appointments:")
            for app in appointments:
                print(f"  - {app.get('appointment_id')} - {app.get('name')} - {app.get('time')}")
        
        current_week.append({
            "day": day,
            "date": date_str,
            "appointments": appointments,
            "is_today": date_str == datetime.now().strftime("%Y-%m-%d")
        })
        
        # Start a new week if we've reached Sunday (weekday 6)
        if len(current_week) == 7:
            calendar_weeks.append(current_week)
            current_week = []
    
    # Add remaining days to complete the last week
    while len(current_week) < 7:
        current_week.append({"day": None, "appointments": []})
    
    if current_week:
        calendar_weeks.append(current_week)
    
    return calendar_weeks

# Calendar Template
calendar_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Calendar - {{ doctor.name }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdn.jsdelivr.net/npm/remixicon@3.5.0/fonts/remixicon.css" rel="stylesheet">
    <style>
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f8fafc;
        }
        .professional-header {
            background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
            border-bottom: 1px solid #475569;
        }
        .professional-sidebar {
            background: #ffffff;
            border-right: 1px solid #e2e8f0;
            box-shadow: 2px 0 4px rgba(0, 0, 0, 0.05);
        }
        .calendar-container {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }
        .calendar-day {
            border: 1px solid #f1f5f9;
            min-height: 120px;
            transition: all 0.2s ease;
            position: relative;
        }
        .calendar-day:hover {
            background: #f8fafc;
            border-color: #cbd5e1;
        }
        .calendar-day.today {
            background: #dbeafe;
            border-color: #3b82f6;
        }
        .calendar-day.today .day-number {
            color: #1d4ed8;
            font-weight: 600;
        }
        .calendar-day.selected {
            background: #eff6ff;
            border-color: #3b82f6;
            box-shadow: inset 0 0 0 2px #3b82f6;
        }
        .appointment-item {
            background: #3b82f6;
            color: white;
            border-radius: 4px;
            padding: 3px 6px;
            margin: 2px 0;
            font-size: 11px;
            cursor: pointer;
            transition: all 0.2s ease;
            border-left: 3px solid #1d4ed8;
        }
        .appointment-item:hover {
            background: #2563eb;
            transform: translateX(2px);
        }
        .appointment-item.scheduled {
            background: #059669;
            border-left-color: #047857;
        }
        .appointment-item.scheduled:hover {
            background: #047857;
        }
        .appointment-item.completed {
            background: #6b7280;
            border-left-color: #4b5563;
        }
        .appointment-item.completed:hover {
            background: #4b5563;
        }
        .professional-button {
            background: #3b82f6;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s ease;
            font-weight: 500;
            font-size: 14px;
        }
        .professional-button:hover {
            background: #2563eb;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3);
        }
        .professional-button.secondary {
            background: #f8fafc;
            color: #475569;
            border: 1px solid #cbd5e1;
        }
        .professional-button.secondary:hover {
            background: #e2e8f0;
            border-color: #94a3b8;
        }
        .professional-input {
            border: 1px solid #cbd5e1;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 14px;
            transition: all 0.2s ease;
            background: #ffffff;
        }
        .professional-input:focus {
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }
        .professional-select {
            border: 1px solid #cbd5e1;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 14px;
            background: #ffffff;
            transition: all 0.2s ease;
        }
        .professional-select:focus {
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }
        .weekday-header {
            background: #f8fafc;
            color: #475569;
            font-weight: 600;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 2px solid #e2e8f0;
        }
        .modal-overlay {
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(4px);
        }
        .modal-content {
            background: white;
            border-radius: 8px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
        }
        .status-indicator {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 8px;
        }
        .status-scheduled { background: #059669; }
        .status-in-progress { background: #3b82f6; }
        
        .status-completed { background: #6b7280; }
        .section-card {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }
        .section-title {
            color: #1e293b;
            font-weight: 600;
            font-size: 16px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .filter-summary {
            background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%);
            border: 1px solid #93c5fd;
            border-radius: 8px;
            padding: 16px;
        }
        .week-highlight {
            background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%) !important;
            border: 2px solid #3b82f6 !important;
            box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2) !important;
            position: relative;
        }
        .week-highlight::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(135deg, rgba(59, 130, 246, 0.1), rgba(29, 78, 216, 0.1));
            pointer-events: none;
        }
        .week-highlight .day-number {
            color: #1d4ed8 !important;
            font-weight: 700 !important;
        }
        .calendar-day.hidden-day {
            opacity: 0.3;
            background: #f8fafc;
            position: relative;
        }
        .calendar-day.hidden-day::before {
            content: 'Hidden by week filter';
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: rgba(0, 0, 0, 0.7);
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 10px;
            white-space: nowrap;
            opacity: 0;
            transition: opacity 0.2s ease;
            pointer-events: none;
            z-index: 10;
        }
        .calendar-day.hidden-day:hover::before {
            opacity: 1;
        }
    </style>
</head>
<body>
    <!-- Professional Header -->
    <header class="professional-header shadow-lg">
        <div class="flex justify-between items-center px-8 py-6">
            <div class="flex items-center space-x-6">
                <div class="flex items-center space-x-4">
                    <div class="w-10 h-10 bg-white bg-opacity-10 rounded-lg flex items-center justify-center">
                        <i class="ri-calendar-line text-white text-xl"></i>
                    </div>
                    <div>
                        <h1 class="text-2xl font-bold text-white">Appointment Calendar</h1>
                        <p class="text-blue-200 text-sm">Dr. {{ doctor.name }} - Medical Practice</p>
                    </div>
                </div>
            </div>
            <div class="flex items-center space-x-4">
                <a href="/dashboard" class="professional-button secondary">
                    <i class="ri-dashboard-line mr-2"></i>Dashboard
                </a>
                <a href="/add_appointment" class="professional-button">
                    <i class="ri-add-line mr-2"></i>New Appointment
                </a>
                <a href="/logout" class="text-white hover:text-blue-200 transition-colors">
                    <i class="ri-logout-box-r-line text-xl"></i>
                </a>
            </div>
        </div>
    </header>

    <div class="flex min-h-screen">
        <!-- Professional Sidebar -->
        <div class="professional-sidebar w-80 p-6">
            <div class="space-y-6">
                <!-- Filter Section -->
                <div class="section-card">
                    <div class="section-title">
                        <i class="ri-filter-3-line text-blue-600"></i>
                        View Options
                    </div>
                    <div class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-700 mb-2">Year</label>
                            <select id="yearFilter" class="professional-select w-full">
                                {% for y in years %}
                                <option value="{{ y }}" {% if y == year %}selected{% endif %}>{{ y }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-700 mb-2">Month</label>
                            <select id="monthFilter" class="professional-select w-full">
                                {% for m_num, m_name in months %}
                                <option value="{{ m_num }}" {% if m_num == month %}selected{% endif %}>{{ m_name }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-700 mb-2">Day (Optional)</label>
                            <input type="number" id="dayFilter" min="1" max="31" placeholder="Enter day" 
                                   value="{{ day if day else '' }}" class="professional-input w-full">
                        </div>
                        <button onclick="updateCalendar()" class="professional-button w-full">
                            <i class="ri-search-line mr-2"></i>Apply Filter
                        </button>
                    </div>
                </div>

                <!-- Quick Actions -->
                <div class="section-card">
                    <div class="section-title">
                        <i class="ri-time-line text-green-600"></i>
                        Quick Actions
                    </div>
                    <div class="space-y-3">
                        <button onclick="goToToday()" class="professional-button w-full text-left">
                            <i class="ri-calendar-line mr-2"></i>Go to Today
                        </button>
                        {% if day %}
                        <button onclick="clearDayFilter()" class="professional-button secondary w-full text-left">
                            <i class="ri-close-line mr-2"></i>Clear Day Filter
                        </button>
                        {% endif %}
                        <button onclick="clearWeekHighlights()" class="professional-button secondary w-full text-left">
                            <i class="ri-filter-off-line mr-2"></i>Clear Week Filter
                        </button>

                        <div class="h-2"></div>
                        <a href="/block_slot" class="professional-button secondary w-full text-left">
                            <i class="ri-lock-2-line mr-2"></i>Block a Slot
                        </a>

                    </div>
                </div>

                <!-- Quick Filters -->
                <div class="section-card">
                    <div class="section-title">
                        <i class="ri-calendar-event-line text-purple-600"></i>
                        Quick Filters
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-2">Choose Range</label>
                        <select id="quickFilterSelect" class="professional-select w-full" onchange="handleQuickFilterChange(this.value)">
                            <option value="" selected>Select...</option>
                            <option value="this_week">This Week</option>
                            <option value="next_week">Next Week</option>
                            <option value="this_month">This Month</option>
                            <option value="next_month">Next Month</option>
                        </select>
                    </div>
                </div>




            </div>
        </div>

        <!-- Main Calendar Area -->
        <div class="flex-1 p-8">
            <!-- Calendar Header -->
            <div class="flex justify-between items-center mb-8">
                <div class="flex items-center space-x-6">
                    <button onclick="navigateMonth(-1)" class="professional-button secondary">
                        <i class="ri-arrow-left-s-line"></i>
                    </button>
                    <h2 class="text-3xl font-bold text-gray-800">{{ month_name }} {{ year }}</h2>
                    <button onclick="navigateMonth(1)" class="professional-button secondary">
                        <i class="ri-arrow-right-s-line"></i>
                    </button>
                </div>
                <div class="text-right">
                    <p class="text-sm text-gray-600">Medical Practice Calendar</p>
                    <p class="text-xs text-gray-500">Professional Appointment Management</p>
                </div>
            </div>

            <!-- Calendar Grid -->
            <div class="calendar-container">
                <table class="w-full">
                    <thead>
                        <tr>
                            <th class="weekday-header p-4 text-left">Monday</th>
                            <th class="weekday-header p-4 text-left">Tuesday</th>
                            <th class="weekday-header p-4 text-left">Wednesday</th>
                            <th class="weekday-header p-4 text-left">Thursday</th>
                            <th class="weekday-header p-4 text-left">Friday</th>
                            <th class="weekday-header p-4 text-left">Saturday</th>
                            <th class="weekday-header p-4 text-left">Sunday</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for week in calendar_data %}
                        <tr>
                            {% for day in week %}
                            <td class="calendar-day p-3 {% if day.day is none %}bg-gray-50{% endif %} {% if day.is_today %}today{% endif %}" 
                                {% if day.day is not none %}onclick="handleDayClick({{ day.day }})" style="cursor: pointer;" title="Click to view this day"{% endif %}>
                                {% if day.day is not none %}
                                <div class="flex justify-between items-start mb-3">
                                    <span class="day-number font-semibold text-lg {% if day.is_today %}text-blue-700{% else %}text-gray-800{% endif %}">
                                        {{ day.day }}
                                    </span>
                                    {% if day.appointments %}
                                    <span class="bg-blue-100 text-blue-800 text-xs px-2 py-1 rounded-full font-medium">
                                        {{ day.appointments|length }}
                                    </span>
                                    {% endif %}
                                </div>
                                
                                {% if day.appointments %}
                                <div class="space-y-2">
                                    {% for appointment in day.appointments %}
                                    <div class="appointment-item {% if appointment.status == 'scheduled' %}scheduled{% elif appointment.status == 'completed' %}completed{% endif %}" 
                                         onclick="handleAppointmentClick('{{ appointment.appointment_id }}'); event.stopPropagation();"
                                         title="{{ appointment.name }} - {{ appointment.time }}">
                                        <div class="font-semibold truncate">{{ appointment.time }}</div>
                                        <div class="truncate opacity-90">{{ appointment.name }}</div>
                                    </div>
                                    {% endfor %}
                                </div>
                                {% endif %}
                                {% endif %}
                            </td>
                            {% endfor %}
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Appointment Modal -->
    <div id="appointmentModal" class="fixed inset-0 modal-overlay hidden z-50">
        <div class="flex items-center justify-center min-h-screen p-4">
            <div class="modal-content max-w-lg w-full max-h-[80vh] overflow-y-auto">
                <div class="flex justify-between items-center p-6 border-b border-gray-200">
                    <h3 class="text-xl font-semibold text-gray-800">Appointment Details</h3>
                    <button onclick="closeAppointmentModal()" class="text-gray-400 hover:text-gray-600">
                        <i class="ri-close-line text-xl"></i>
                    </button>
                </div>
                <div id="appointmentModalContent" class="p-6">
                    <!-- Content will be loaded here -->
                </div>
            </div>
        </div>
    </div>

    <script>
        function updateCalendar() {
            const year = document.getElementById('yearFilter').value;
            const month = document.getElementById('monthFilter').value;
            const day = document.getElementById('dayFilter').value;
            
            console.log('Updating calendar:', { year, month, day });
            
            // Clear any stored week filter when manually updating calendar
            sessionStorage.removeItem('filterWeek');
            
            let url = `/calendar?year=${year}&month=${month}`;
            if (day && day.trim() !== '') {
                url += `&day=${day}`;
            }
            console.log('Navigating to:', url);
            window.location.href = url;
        }

        function navigateMonth(direction) {
            const year = parseInt(document.getElementById('yearFilter').value);
            const month = parseInt(document.getElementById('monthFilter').value);
            const day = document.getElementById('dayFilter').value;
            
            console.log('Navigating month:', { direction, year, month, day });
            
            let newMonth = month + direction;
            let newYear = year;
            
            if (newMonth > 12) {
                newMonth = 1;
                newYear++;
            } else if (newMonth < 1) {
                newMonth = 12;
                newYear--;
            }
            
            // Clear any stored week filter when navigating to a different month
            sessionStorage.removeItem('filterWeek');
            
            let url = `/calendar?year=${newYear}&month=${newMonth}`;
            if (day && day.trim() !== '') {
                url += `&day=${day}`;
            }
            console.log('Navigating to:', url);
            window.location.href = url;
        }

        function goToToday() {
            console.log('Go to Today clicked');
            const today = new Date();
            const year = today.getFullYear();
            const month = today.getMonth() + 1;
            const day = today.getDate();
            
            // Clear any stored week filter when going to today
            sessionStorage.removeItem('filterWeek');
            
            console.log('Navigating to today:', { year, month, day });
            window.location.href = `/calendar?year=${year}&month=${month}&day=${day}`;
        }

        function clearDayFilter() {
            console.log('Clear day filter clicked');
            const year = document.getElementById('yearFilter').value;
            const month = document.getElementById('monthFilter').value;
            
            // Clear any stored week filter when clearing day filter
            sessionStorage.removeItem('filterWeek');
            
            console.log('Clearing day filter:', { year, month });
            window.location.href = `/calendar?year=${year}&month=${month}`;
        }

        function setQuickFilter(filterType) {
            console.log('Quick filter clicked:', filterType);
            
            try {
                const today = new Date();
                let targetDate = new Date(today);
                
                console.log('Today:', today);
                console.log('Target date:', targetDate);
                
                switch(filterType) {
                    case 'this_week':
                        console.log('Processing this_week case');
                        // Calculate the Monday of current week
                        const currentDayOfWeek = today.getDay();
                        const daysToMonday = currentDayOfWeek === 0 ? 6 : currentDayOfWeek - 1;
                        const mondayOfThisWeek = new Date(today);
                        mondayOfThisWeek.setDate(today.getDate() - daysToMonday);
                        
                        // Navigate to the current month and filter to show only this week's appointments
                        const currentYear = today.getFullYear();
                        const currentMonth = today.getMonth() + 1;
                        const currentWeekUrl = `/calendar?year=${currentYear}&month=${currentMonth}`;
                        console.log('Navigating to current month:', currentWeekUrl);
                        
                        // Store the week to filter after navigation
                        sessionStorage.setItem('filterWeek', mondayOfThisWeek.toISOString());
                        window.location.href = currentWeekUrl;
                        return;
                    case 'next_week':
                        console.log('Processing next_week case');
                        // Calculate the Monday of next week
                        const nextWeekDay = today.getDay();
                        const daysToNextMonday = nextWeekDay === 0 ? 1 : 8 - nextWeekDay;
                        const mondayOfNextWeek = new Date(today);
                        mondayOfNextWeek.setDate(today.getDate() + daysToNextMonday);
                        
                        // Navigate to the month containing next week and filter appointments
                        const nextWeekYear = mondayOfNextWeek.getFullYear();
                        const nextWeekMonth = mondayOfNextWeek.getMonth() + 1;
                        const nextWeekUrl = `/calendar?year=${nextWeekYear}&month=${nextWeekMonth}`;
                        console.log('Navigating to:', nextWeekUrl);
                        
                        // Store the week to filter after navigation
                        sessionStorage.setItem('filterWeek', mondayOfNextWeek.toISOString());
                        window.location.href = nextWeekUrl;
                        return;
                    case 'this_month':
                        console.log('Processing this_month case');
                        // Show all appointments in current month
                        const thisYear = today.getFullYear();
                        const thisMonth = today.getMonth() + 1;
                        const thisMonthUrl = `/calendar?year=${thisYear}&month=${thisMonth}`;
                        console.log('Navigating to current month:', thisMonthUrl);
                        window.location.href = thisMonthUrl;
                        return;
                    case 'next_month':
                        console.log('Processing next_month case');
                        targetDate.setMonth(today.getMonth() + 1);
                        // For next month, show the full month without day filter
                        const nextYear = targetDate.getFullYear();
                        const nextMonth = targetDate.getMonth() + 1;
                        console.log('Next month filter:', { nextYear, nextMonth });
                        const nextMonthUrl = `/calendar?year=${nextYear}&month=${nextMonth}`;
                        console.log('Navigating to:', nextMonthUrl);
                        window.location.href = nextMonthUrl;
                        return;
                    default:
                        console.log('Unknown filter type:', filterType);
                        return;
                }
            } catch (error) {
                console.error('Error in setQuickFilter:', error);
                alert('Error in quick filter: ' + error.message);
            }
        }

        function handleQuickFilterChange(value) {
            if (!value) { return; }
            setQuickFilter(value);
            // Reset select back to placeholder after navigation trigger
            const select = document.getElementById('quickFilterSelect');
            if (select) {
                select.value = '';
            }
        }

        function filterWeekAppointments(mondayDate) {
            console.log('Filtering appointments for week starting from:', mondayDate);
            
            // Clear any existing highlights and filters
            clearWeekHighlights();
            clearWeekFilters();
            
            // Calculate all days in the week (Monday to Sunday)
            const weekDays = [];
            for (let i = 0; i < 7; i++) {
                const dayDate = new Date(mondayDate);
                dayDate.setDate(mondayDate.getDate() + i);
                weekDays.push(dayDate);
            }
            
            console.log('Week days to show:', weekDays);
            
            // Get all calendar day cells
            const allDayCells = document.querySelectorAll('.calendar-day');
            
            allDayCells.forEach(cell => {
                const daySpan = cell.querySelector('.day-number');
                if (daySpan && daySpan.textContent.trim()) {
                    const dayNumber = parseInt(daySpan.textContent.trim());
                    
                    // Check if this day is in our target week
                    const isInTargetWeek = weekDays.some(weekDay => weekDay.getDate() === dayNumber);
                    
                    if (isInTargetWeek) {
                        // Show this day and its appointments
                        cell.style.display = 'table-cell';
                        cell.classList.add('week-highlight');
                        cell.classList.remove('hidden-day');
                        
                        // Show all appointment items in this day
                        const appointmentItems = cell.querySelectorAll('.appointment-item');
                        appointmentItems.forEach(item => {
                            item.style.display = 'block';
                        });
                    } else {
                        // Mark this day as hidden but keep it visible with reduced opacity
                        cell.style.display = 'table-cell';
                        cell.classList.add('hidden-day');
                        cell.classList.remove('week-highlight');
                        
                        // Hide appointment items in this day
                        const appointmentItems = cell.querySelectorAll('.appointment-item');
                        appointmentItems.forEach(item => {
                            item.style.display = 'none';
                        });
                    }
                }
            });
            
            // Add a visual indicator for the filtered week
            addWeekFilterIndicator(mondayDate);
        }

        function showAllAppointments() {
            console.log('Showing all appointments');
            
            // Clear any existing highlights and filters
            clearWeekHighlights();
            clearWeekFilters();
            
            // Show all calendar day cells
            const allDayCells = document.querySelectorAll('.calendar-day');
            allDayCells.forEach(cell => {
                cell.style.display = 'table-cell';
                cell.classList.remove('hidden-day');
                
                // Show all appointment items in this day
                const appointmentItems = cell.querySelectorAll('.appointment-item');
                appointmentItems.forEach(item => {
                    item.style.display = 'block';
                });
            });
            
            // Remove week filter indicator
            const existingIndicator = document.querySelector('.week-filter-indicator');
            if (existingIndicator) {
                existingIndicator.remove();
            }
            
            // Clear stored week filter
            sessionStorage.removeItem('filterWeek');
        }

        function clearWeekHighlights() {
            // Remove existing week highlights
            document.querySelectorAll('.week-highlight').forEach(cell => {
                cell.classList.remove('week-highlight');
            });
            
            // Remove hidden day styling
            document.querySelectorAll('.hidden-day').forEach(cell => {
                cell.classList.remove('hidden-day');
            });
            
            // Remove week indicator
            const existingIndicator = document.querySelector('.week-indicator');
            if (existingIndicator) {
                existingIndicator.remove();
            }
            
            // Clear stored week filter
            sessionStorage.removeItem('filterWeek');
        }

        function clearWeekFilters() {
            // Remove week filter indicator
            const existingIndicator = document.querySelector('.week-filter-indicator');
            if (existingIndicator) {
                existingIndicator.remove();
            }
            
            // Clear stored week filter
            sessionStorage.removeItem('filterWeek');
        }

        function addWeekIndicator(mondayDate) {
            // Create a visual indicator for the highlighted week
            const indicator = document.createElement('div');
            indicator.className = 'week-indicator';
            indicator.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                background: linear-gradient(135deg, #3b82f6, #1d4ed8);
                color: white;
                padding: 12px 20px;
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3);
                z-index: 1000;
                font-weight: 600;
                font-size: 14px;
            `;
            
            const weekStart = mondayDate.toLocaleDateString('en-US', { 
                month: 'short', 
                day: 'numeric' 
            });
            const weekEnd = new Date(mondayDate);
            weekEnd.setDate(mondayDate.getDate() + 6);
            const weekEndStr = weekEnd.toLocaleDateString('en-US', { 
                month: 'short', 
                day: 'numeric' 
            });
            
            indicator.innerHTML = `
                <div style="display: flex; align-items: center; gap: 8px;">
                    <i class="ri-calendar-week-line"></i>
                    <span>Week: ${weekStart} - ${weekEndStr}</span>
                    <button onclick="clearWeekHighlights()" style="background: none; border: none; color: white; cursor: pointer; margin-left: 8px;">
                        <i class="ri-close-line"></i>
                    </button>
                </div>
            `;
            
            document.body.appendChild(indicator);
        }

        function addWeekFilterIndicator(mondayDate) {
            // Create a visual indicator for the filtered week
            const indicator = document.createElement('div');
            indicator.className = 'week-filter-indicator';
            indicator.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                background: linear-gradient(135deg, #059669, #047857);
                color: white;
                padding: 12px 20px;
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(5, 150, 105, 0.3);
                z-index: 1000;
                font-weight: 600;
                font-size: 14px;
            `;
            
            const weekStart = mondayDate.toLocaleDateString('en-US', { 
                month: 'short', 
                day: 'numeric' 
            });
            const weekEnd = new Date(mondayDate);
            weekEnd.setDate(mondayDate.getDate() + 6);
            const weekEndStr = weekEnd.toLocaleDateString('en-US', { 
                month: 'short', 
                day: 'numeric' 
            });
            
            indicator.innerHTML = `
                <div style="display: flex; align-items: center; gap: 8px;">
                    <i class="ri-filter-3-line"></i>
                    <span>Showing Week: ${weekStart} - ${weekEndStr}</span>
                    <button onclick="clearWeekHighlights()" style="background: none; border: none; color: white; cursor: pointer; margin-left: 8px;">
                        <i class="ri-close-line"></i>
                    </button>
                </div>
            `;
            
            document.body.appendChild(indicator);
        }

        function handleDayClick(day) {
            console.log('Day clicked:', day);
            
            // Check if there's an active week filter
            const hasWeekFilter = document.querySelector('.week-filter-indicator') !== null;
            const hasWeekHighlight = document.querySelector('.week-highlight') !== null;
            const hasHiddenDays = document.querySelector('.hidden-day') !== null;
            
            // Check if the clicked day is currently hidden
            const clickedCell = event.target.closest('.calendar-day');
            const isHiddenDay = clickedCell && clickedCell.classList.contains('hidden-day');
            
            if (hasWeekFilter || hasWeekHighlight || hasHiddenDays || isHiddenDay) {
                // If there's a week filter active or the clicked day is hidden, clear it first
                console.log('Week filter active or hidden day clicked, clearing filter first');
                clearWeekHighlights();
                // Then filter by the clicked day
                setTimeout(() => {
                    filterByDay(day);
                }, 100);
            } else {
                // No week filter, just filter by day normally
                filterByDay(day);
            }
        }

        function filterByDay(day) {
            console.log('Filter by day clicked:', day);
            const year = document.getElementById('yearFilter').value;
            const month = document.getElementById('monthFilter').value;
            
            // Clear any stored week filter when filtering by day
            sessionStorage.removeItem('filterWeek');
            
            // Also clear any active week highlights
            clearWeekHighlights();
            
            console.log('Filtering by day:', { year, month, day });
            window.location.href = `/calendar?year=${year}&month=${month}&day=${day}`;
        }

        function handleAppointmentClick(appointmentId) {
            console.log('Appointment clicked:', appointmentId);
            
            // Check if there's an active week filter
            const hasWeekFilter = document.querySelector('.week-filter-indicator') !== null;
            const hasWeekHighlight = document.querySelector('.week-highlight') !== null;
            const hasHiddenDays = document.querySelector('.hidden-day') !== null;
            
            if (hasWeekFilter || hasWeekHighlight || hasHiddenDays) {
                // If there's a week filter active, clear it first
                console.log('Week filter active, clearing it first');
                clearWeekHighlights();
                // Then show the appointment modal
                setTimeout(() => {
                    showAppointmentModal(appointmentId);
                }, 100);
            } else {
                // No week filter, just show the appointment modal normally
                showAppointmentModal(appointmentId);
            }
        }

        function showAppointmentModal(appointmentId) {
            console.log('Opening modal for appointment:', appointmentId);
            
            // Check if modal elements exist
            const modal = document.getElementById('appointmentModal');
            const modalContent = document.getElementById('appointmentModalContent');
            
            if (!modal) {
                console.error('Modal element not found');
                alert('Modal element not found');
                return;
            }
            
            if (!modalContent) {
                console.error('Modal content element not found');
                alert('Modal content element not found');
                return;
            }
            
            console.log('Modal elements found, making fetch request...');
            
            fetch(`/get_appointment_details/${appointmentId}`)
                .then(response => {
                    console.log('Response status:', response.status);
                    console.log('Response headers:', response.headers);
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    console.log('Appointment data received:', data);
                    console.log('Data type:', typeof data);
                    console.log('Data keys:', Object.keys(data));
                    
                    if (data.success) {
                        const appointment = data.appointment;
                        console.log('Appointment object:', appointment);
                        console.log('Appointment keys:', Object.keys(appointment));
                        
                        // Log specific fields to debug
                        console.log('Name:', appointment.name);
                        console.log('Phone:', appointment.phone);
                        console.log('Email:', appointment.email);
                        console.log('Address:', appointment.address);
                        console.log('Date:', appointment.date);
                        console.log('Date display:', appointment.date_display);
                        console.log('Date formatted:', appointment.date_formatted);
                        console.log('Time:', appointment.time);
                        console.log('Symptoms:', appointment.symptoms);
                        console.log('Status:', appointment.status);
                        console.log('Created at:', appointment.created_at_str);
                        
                        modalContent.innerHTML = `
                            <div class="space-y-6">
                                <!-- Patient Header -->
                                <div class="bg-gradient-to-r from-blue-50 to-indigo-50 p-6 rounded-lg border border-blue-200">
                                    <div class="flex items-center space-x-4">
                                        <div class="w-16 h-16 bg-gradient-to-br from-blue-600 to-indigo-700 rounded-xl flex items-center justify-center shadow-lg">
                                            <i class="ri-user-heart-line text-white text-2xl"></i>
                                        </div>
                                        <div class="flex-1">
                                            <h3 class="text-2xl font-bold text-gray-900 mb-1">${appointment.name || 'N/A'}</h3>
                                            <p class="text-blue-600 font-medium">Patient Details</p>
                                            <p class="text-sm text-gray-600 mt-1">Appointment ID: ${appointment.appointment_id || 'N/A'}</p>
                                        </div>
                                        <div class="text-right">
                                            <div class="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium bg-blue-100 text-blue-800">
                                                <span class="status-indicator status-${appointment.status || 'pending'} mr-2"></span>
                                                ${(appointment.status || 'pending').replace('_', ' ').toUpperCase()}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                
                                <!-- Contact Information -->
                                <div class="bg-white border border-gray-200 rounded-lg p-6">
                                    <h4 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
                                        <i class="ri-contacts-line text-blue-600 mr-2"></i>
                                        Contact Information
                                    </h4>
                                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                        <div class="flex items-center space-x-3 p-3 bg-gray-50 rounded-lg">
                                            <i class="ri-phone-line text-green-600 text-lg"></i>
                                            <div>
                                                <p class="text-sm text-gray-600">Phone</p>
                                                <p class="font-medium text-gray-900">${appointment.phone || 'N/A'}</p>
                                            </div>
                                        </div>
                                        <div class="flex items-center space-x-3 p-3 bg-gray-50 rounded-lg">
                                            <i class="ri-mail-line text-blue-600 text-lg"></i>
                                            <div>
                                                <p class="text-sm text-gray-600">Email</p>
                                                <p class="font-medium text-gray-900">${appointment.email || 'N/A'}</p>
                                            </div>
                                        </div>
                                        <div class="flex items-start space-x-3 p-3 bg-gray-50 rounded-lg md:col-span-2">
                                            <i class="ri-map-pin-line text-red-600 text-lg mt-1"></i>
                                            <div>
                                                <p class="text-sm text-gray-600">Address</p>
                                                <p class="font-medium text-gray-900">${appointment.address || 'No address provided'}</p>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                
                                <!-- Appointment Details -->
                                <div class="bg-white border border-gray-200 rounded-lg p-6">
                                    <h4 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
                                        <i class="ri-calendar-event-line text-purple-600 mr-2"></i>
                                        Appointment Details
                                    </h4>
                                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                        <div class="flex items-center space-x-3 p-3 bg-gray-50 rounded-lg">
                                            <i class="ri-calendar-line text-purple-600 text-lg"></i>
                                            <div>
                                                <p class="text-sm text-gray-600">Date</p>
                                                <p class="font-medium text-gray-900">${appointment.date_display || appointment.date || 'N/A'}</p>
                                            </div>
                                        </div>
                                        <div class="flex items-center space-x-3 p-3 bg-gray-50 rounded-lg">
                                            <i class="ri-time-line text-purple-600 text-lg"></i>
                                            <div>
                                                <p class="text-sm text-gray-600">Time</p>
                                                <p class="font-medium text-gray-900">${appointment.time || 'N/A'}</p>
                                            </div>
                                        </div>

                                    </div>
                                </div>
                                
                                <!-- Medical Information -->
                                <div class="bg-white border border-gray-200 rounded-lg p-6">
                                    <h4 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
                                        <i class="ri-heart-pulse-line text-red-600 mr-2"></i>
                                        Medical Information
                                    </h4>
                                    <div class="bg-red-50 border border-red-200 rounded-lg p-4">
                                        <div class="flex items-start space-x-3">
                                            <i class="ri-stethoscope-line text-red-600 text-lg mt-1"></i>
                                            <div class="flex-1">
                                                <p class="text-sm text-gray-600 mb-2">Symptoms & Medical Notes</p>
                                                <p class="text-gray-900 leading-relaxed">${appointment.symptoms || 'No medical notes available'}</p>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                
                                <!-- Action Buttons -->
                                <div class="flex space-x-4 pt-4">
                                    <a href="/edit_appointment/${appointment.appointment_id}" 
                                       class="professional-button flex-1 text-center bg-blue-600 hover:bg-blue-700">
                                        <i class="ri-edit-line mr-2"></i>Edit Appointment
                                    </a>
                                    <a href="/add_prescription?patient_phone=${encodeURIComponent(appointment.phone)}" 
                                       class="professional-button flex-1 text-center bg-green-600 hover:bg-green-700">
                                        <i class="ri-medicine-bottle-line mr-2"></i>Add Prescription
                                    </a>
                                    <button onclick="closeAppointmentModal()" 
                                            class="professional-button secondary flex-1">
                                        <i class="ri-close-line mr-2"></i>Close
                                    </button>
                                </div>
                            </div>
                        `;
                        
                        document.getElementById('appointmentModal').classList.remove('hidden');
                    } else {
                        alert('Error loading appointment details');
                    }
                })
                .catch(error => {
                    console.error('Error loading appointment details:', error);
                    alert('Error loading appointment details: ' + error.message);
                });
        }

        function closeAppointmentModal() {
            document.getElementById('appointmentModal').classList.add('hidden');
        }



        // Highlight selected day on page load
        function highlightSelectedDay() {
            const dayFilter = document.getElementById('dayFilter').value;
            if (dayFilter) {
                const dayCells = document.querySelectorAll('.calendar-day');
                dayCells.forEach(cell => {
                    const daySpan = cell.querySelector('.day-number');
                    if (daySpan && daySpan.textContent.trim() === dayFilter) {
                        cell.classList.add('selected');
                    }
                });
            }
        }

        // Close modal when clicking outside
        document.getElementById('appointmentModal').addEventListener('click', function(e) {
            if (e.target === this) {
                closeAppointmentModal();
            }
        });

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            console.log('Calendar page loaded');
            highlightSelectedDay();
            
            // Check for stored week filter (for next week navigation)
            const storedFilterWeek = sessionStorage.getItem('filterWeek');
            if (storedFilterWeek) {
                console.log('Found stored week to filter:', storedFilterWeek);
                const mondayDate = new Date(storedFilterWeek);
                // Clear the stored value
                sessionStorage.removeItem('filterWeek');
                // Filter the week after a short delay to ensure DOM is ready
                setTimeout(() => {
                    // First show all appointments, then filter
                    showAllAppointments();
                    filterWeekAppointments(mondayDate);
                }, 100);
            } else {
                // If no stored filter, ensure all appointments are visible
                setTimeout(() => {
                    showAllAppointments();
                }, 50);
            }
            
            // Add click event listeners for debugging
            document.querySelectorAll('.professional-button').forEach(button => {
                button.addEventListener('click', function(e) {
                    console.log('Button clicked:', this.textContent.trim());
                });
            });
            
            // Debug filter elements
            console.log('Filter elements found:');
            console.log('Year filter:', document.getElementById('yearFilter'));
            console.log('Month filter:', document.getElementById('monthFilter'));
            console.log('Day filter:', document.getElementById('dayFilter'));
            
            // Debug quick action buttons
            console.log('Quick action buttons found:', document.querySelectorAll('[onclick*="goToToday"]').length);
            console.log('Quick filter buttons found:', document.querySelectorAll('[onclick*="setQuickFilter"]').length);
            
            // Debug appointment items
            console.log('Appointment items found:', document.querySelectorAll('.appointment-item').length);
            
            // Add error handling for missing elements
            const yearFilter = document.getElementById('yearFilter');
            const monthFilter = document.getElementById('monthFilter');
            const dayFilter = document.getElementById('dayFilter');
            
            if (!yearFilter) console.error('Year filter not found');
            if (!monthFilter) console.error('Month filter not found');
            if (!dayFilter) console.error('Day filter not found');
            
            // Test quick filter buttons
            document.querySelectorAll('[onclick*="setQuickFilter"]').forEach((button, index) => {
                console.log(`Quick filter button ${index}:`, button.textContent.trim());
            });
            
            // Test quick action buttons
            document.querySelectorAll('[onclick*="goToToday"]').forEach((button, index) => {
                console.log(`Quick action button ${index}:`, button.textContent.trim());
            });
        });
    </script>
</body>
</html>
"""

# API endpoint to get appointment details for modal
@app.route("/get_appointment_details/<appointment_id>")
def get_appointment_details(appointment_id):
    print(f"=== GET APPOINTMENT DETAILS CALLED ===")
    print(f"Appointment ID: {appointment_id}")
    print(f"Session: {session}")
    
    if "doctor" not in session:
        print("Not authenticated - doctor not in session")
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    
    try:
        print(f"Looking for appointment in database: {appointment_id}")
        appointment = appointments_collection.find_one({"appointment_id": appointment_id})
        
        if appointment:
            print(f"Found appointment: {appointment}")
            print(f"Appointment keys: {list(appointment.keys())}")
            
            # Convert ObjectId to string for JSON serialization
            if '_id' in appointment:
                appointment['_id'] = str(appointment['_id'])
            
            # Ensure date is properly formatted for display
            if 'date' in appointment and appointment['date']:
                try:
                    # Parse the date and format it nicely
                    from datetime import datetime
                    date_obj = datetime.strptime(appointment['date'], '%Y-%m-%d')
                    appointment['date_display'] = date_obj.strftime('%d %B %Y')  # e.g., "15 January 2024"
                    appointment['date_formatted'] = date_obj.strftime('%d-%m-%Y')  # e.g., "15-01-2024"
                except Exception as e:
                    print(f"Error formatting date {appointment['date']}: {e}")
                    appointment['date_display'] = appointment['date']
                    appointment['date_formatted'] = appointment['date']
            else:
                appointment['date_display'] = 'N/A'
                appointment['date_formatted'] = 'N/A'
            
            print(f"Appointment date fields:")
            print(f"  Original date: {appointment.get('date')}")
            print(f"  Date display: {appointment.get('date_display')}")
            print(f"  Date formatted: {appointment.get('date_formatted')}")
            
            response_data = {"success": True, "appointment": appointment}
            print(f"Returning response: {response_data}")
            return jsonify(response_data)
        else:
            print(f"Appointment not found: {appointment_id}")
            # Let's also check what appointments exist
            all_appointments = list(appointments_collection.find({}, {"appointment_id": 1, "name": 1}))
            print(f"All appointments in database: {all_appointments}")
            return jsonify({"success": False, "error": "Appointment not found"}), 404
    except Exception as e:
        print(f"Error getting appointment details: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    login_template = """
    <!DOCTYPE html>
    <html lang="en" class="bg-gray-100">
    <head>
        <meta charset="UTF-8">
        <title>Doctor Login</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="flex items-center justify-center min-h-screen bg-gray-100">
        <div class="bg-white p-8 rounded-lg shadow-md w-full max-w-sm">
            <h2 class="text-2xl font-bold mb-6 text-center text-gray-800">Doctor Login</h2>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% for category, message in messages %}
                    <div class="mb-4 text-sm p-3 rounded bg-red-100 text-red-800">
                        {{ message }}
                    </div>
                {% endfor %}
            {% endwith %}
            <form method="POST" action="/login">
                <div class="mb-4">
                    <label for="username" class="block text-gray-700 text-sm font-bold mb-2">Username:</label>
                    <input type="text" id="username" name="username" required
                           class="shadow appearance-none border rounded w-full py-2 px-3 text-gray-700 leading-tight focus:outline-none focus:shadow-outline">
                </div>
                <div class="mb-6">
                    <label for="password" class="block text-gray-700 text-sm font-bold mb-2">Password:</label>
                    <input type="password" id="password" name="password" required
                           class="shadow appearance-none border rounded w-full py-2 px-3 text-gray-700 mb-3 leading-tight focus:outline-none focus:shadow-outline">
                </div>
                <div class="flex items-center justify-between">
                    <button type="submit"
                            class="bg-teal-600 hover:bg-teal-700 text-white font-bold py-2 px-4 rounded focus:outline-none focus:shadow-outline">
                        Login
                    </button>
                    <a href="/" class="inline-block align-baseline font-bold text-sm text-teal-600 hover:text-teal-800">
                        Back to Home
                    </a>
                </div>
            </form>
        </div>
    </body>
    </html>
    """
    if doctors_collection.count_documents({}) == 0:
        doctors_collection.insert_one({"username": "drpriya", "password": "password123"})
        print("Default doctor 'drpriya' created with password 'password123'. Please change this in production!")
    
    app.run(debug=True)
    
    