from flask import Flask, render_template, request, redirect, url_for
from pymongo import MongoClient
import bcrypt
import os
import datetime

from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)

client = MongoClient("mongodb://localhost:27017/")
db = client['Furniture_E_shop']
customers_collection = db['Customer']
sellers_collection = db['Seller']
admins_collection = db['Admin']
product_collection = db['Products']
variant_collection = db['Variants']
category_collection = db['Category']
orders_collection = db['Orders']
payments_collection = db['Payments']
  # assuming admin data is pre-loaded
app.secret_key = 'furniture_online'
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        role = request.form['role']
        email = request.form['email']
        password = request.form['password']

        collection = None
        if role == 'buyer':
            collection = customers_collection
        elif role == 'seller':
            collection = sellers_collection
        elif role == 'admin':
            collection = admins_collection

        user = collection.find_one({"email": email})
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
            # Login success - save to session
            session['role'] = role
            session['email'] = user['email']
            session['name'] = user['name']

            # Redirect based on role
            if role == 'buyer':
                return redirect(url_for('buyer_dashboard'))
            elif role == 'seller':
                return redirect(url_for('seller_dashboard'))
            elif role == 'admin':
                return redirect(url_for('admin_dashboard'))
        else:
            error = "Invalid credentials. Please try again."

    return render_template('login.html', error=error)

@app.route('/buyer_dashboard')
def buyer_dashboard():
        return render_template('customer_dashboard.html', name=session.get('name'))

@app.route('/seller_dashboard')
def seller_dashboard():
    if 'role' not in session or session['role'] != 'seller':
        return redirect(url_for('login'))
    return render_template('seller_dashboard.html', name=session.get('name'))

@app.route('/admin_dashboard')
def admin_dashboard():
    return render_template('admin_dashbaord.html', name=session.get('name'))


@app.route('/', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        mobile = request.form['mobile']
        address = request.form['address']
        street = request.form['street']
        state = request.form['state']
        zip_code = request.form['zip']
        role = request.form['role']

        # Combine address into a single string
        full_address = f"{address}, {street}, {state}, {zip_code}"

        # Hash password
        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        # User document
        user_data = {
            "name": name,
            "email": email,
            "password": hashed_pw,
            "mobile": mobile,
            "address": full_address,
            "role": role,
            "cart":[]
        }

        # Save to correct collection
        if role == "customer":
            customers_collection.insert_one(user_data)
        elif role == "seller":
            sellers_collection.insert_one(user_data)

        return redirect(url_for('register'))

    return render_template('index.html')

UPLOAD_FOLDER = os.path.join('static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}

'''app.config['UPLOAD_FOLDER'] = "static/uploads"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}'''

# Function to check allowed file extensions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

from bson import ObjectId

@app.route('/create_product', methods=['GET', 'POST'])
def create_product():
    if request.method == 'POST':
        # Ensure user is logged in
        if 'email' not in session:
            return redirect(url_for('login'))

        # Get product data from form
        product_name = request.form['product_name']
        price = float(request.form['price'])
        quantity = int(request.form['quantity'])
        category_id = request.form['category']
        color = request.form['color']
        material = request.form['material']
        dimension = request.form['dimension']
        brand = request.form['brand']


        # Get seller email from session
        seller_email = session['email']

        # Handle image upload
        image = request.files['image']
        image_path = None
        if image and allowed_file(image.filename):
            image_filename = secure_filename(image.filename)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_filename)
            image.save(image_path)

        # Fetch category details using ObjectId
        try:
            category_obj_id = ObjectId(category_id)
        except:
            return "Invalid category ID!", 400

        category_details = category_collection.find_one({"_id": category_obj_id})
        if not category_details:
            return "Category not found!", 404

        # Create variant
        variant_data = {
            "color": color,
            "material": material,
            "dimension": dimension,
            "brand": brand,
          
        }
        variant_id = variant_collection.insert_one(variant_data).inserted_id

        # Create product document
        product_data = {
            "name": product_name,
            "price": price,
            "quantity": quantity,
            "seller": seller_email,
            "image": image_path,
            "category_id": category_obj_id,
            "variant_id": variant_id,
        }

        product_collection.insert_one(product_data)

        return redirect(url_for('seller_dashboard'))

    # Fetch categories for dropdown
    categories = category_collection.find()
    return render_template('create_product.html', categories=categories)

@app.route("/buyer_view_product/<product_id>")
def view_product(product_id):
    product = product_collection.find_one({"_id": ObjectId(product_id)})

    if not product:
        return "Product not found", 404

    # Fetch related variant and category
    variant = variant_collection.find_one({"_id": ObjectId(product.get("variant_id"))})
    category = category_collection.find_one({"_id": ObjectId(product.get("category_id"))})

    return render_template(
        "buyer_view_product.html",
        product=product,
        variant=variant,
        category=category
    )

@app.route('/browse_products')
def browse_products():
    products = list(product_collection.find({"quantity": {"$gt": 0}}))
    return render_template('browse_products.html', products=products)
@app.route('/logout')
def logout():
    # Clear the session
    session.pop('role', None)  # Remove the role from session
    session.pop('email', None)  # Remove the email from session
    session.pop('name', None)  # Remove the name from session
    
    # Redirect to the login page
    return redirect(url_for('login'))


@app.route('/add_to_cart', methods=['POST'])
def add_to_cart():
    # Retrieve product ID and quantity from the form
    product_id = request.form.get('product_id')
    quantity = int(request.form.get('quantity'))
    
    # Ensure the user is logged in (i.e., user_id is in session)
    mail = session.get('email')
    if not mail:
        return redirect(url_for('login'))  # Redirect to login if user is not logged in
    
    # Fetch the user from the Buyer collection using the user_id (using PyMongo)
    user = customers_collection.find_one({'email': mail})
    if not user:
        return redirect(url_for('login'))  # Redirect if no user found
    
    # Initialize the cart if not already in the user document
    if 'cart' not in user:
        user['cart'] = []

    product_exists = False
    for item in user['cart']:
        if str(item['product_id']) == product_id:
            item['quantity'] += quantity
            product_exists = True
            break

    if not product_exists:
        user['cart'].append({'product_id': product_id, 'quantity': quantity})
    
    customers_collection.update_one({'email': mail}, {'$set': {'cart': user['cart']}})

    return redirect(url_for('browse_products'))


@app.route('/view_cart')
def view_cart():
    # Retrieve the user email from the session
    mail = session.get('email')
    if not mail:
        return redirect(url_for('login'))  # Redirect to login if user is not logged in

    user = customers_collection.find_one({'email': mail})
    if not user or 'cart' not in user:
        return redirect(url_for('home'))  # Redirect to home if cart is empty

    cart_items = []
    grand_total = 0
    disable_checkout_button = False  # Flag to disable checkout button

    for item in user['cart']:
        product = product_collection.find_one({'_id': ObjectId(item['product_id'])})

        if product:
            category = category_collection.find_one({'_id': ObjectId(product['category_id'])})
            variant = variant_collection.find_one({'_id': ObjectId(product['variant_id'])})

            item_total = product['price'] * item['quantity']
            grand_total += item_total

            # Check if cart quantity exceeds product availability
            if item['quantity'] > product['quantity']:
                item_status = f"Only {product['quantity']} available"
                disable_checkout_button = True  # Disable checkout if any item exceeds stock
            else:
                item_status = None

            cart_items.append({
                'product_name': product['name'],
                'product_id': product['_id'],
                'quantity': item['quantity'],
                'image': product['image'],
                'price': product['price'],
                'total': item_total,
                'category': category,
                'variant': variant,
                'status': item_status  # Add status to each item
            })

    return render_template('view_cart.html', cart_items=cart_items, grand_total=grand_total, disable_checkout_button=disable_checkout_button)





@app.route('/remove_from_cart/<product_id>', methods=['GET'])
def remove_from_cart(product_id):
    mail = session.get('email')  # Assume user_id is stored in session


    user = customers_collection.find_one({'email': mail})

    customers_collection.update_one(
        {'email': mail},
        {'$pull': {'cart': {'product_id': product_id}}}
    )


    return redirect(url_for('view_cart'))


@app.route('/user_payment')
def user_payment():
    mail = session.get('email')
    if not mail:
        return redirect(url_for('login'))

    user = customers_collection.find_one({'email': mail})
    if not user or 'cart' not in user:
        return redirect(url_for('home'))

    total = 0
    for item in user['cart']:
        product = product_collection.find_one({'_id': ObjectId(item['product_id'])})
        if product:
            total += product['price'] * item['quantity']

    tax = round(total * 0.18, 2)
    platform_fee = round(total * 0.02, 2)
    final_total = round(total + tax + platform_fee, 2)

    return render_template(
        'payment.html',
        total=total,
        tax=tax,
        platform_fee=platform_fee,
        final_total=final_total
    )

@app.route('/process_payment', methods=['POST'])
def process_payment():
    # Retrieve the user email from the session
    email = session.get('email')
    if not email:
        return redirect(url_for('login'))  # Redirect to login if user is not logged in

    # Get the user's cart
    user = customers_collection.find_one({'email': email})
    if not user or 'cart' not in user or len(user['cart']) == 0:
        return redirect(url_for('view_cart'))  # Redirect to cart if empty

    # Retrieve products from cart
    products = user['cart']
    
    # Get payment details from the form
    card_name = request.form['name']
    card_number = request.form['card_number']
    card_cvv = request.form['cvv']
    card_exp = request.form['exp_date']
    
    # Get delivery address details from the form
    delivery_address = {
        'address': request.form['address'],
        'street': request.form['street'],
        'state': request.form['state'],
        'zip': request.form['zip']
    }
    supplier_name=''
    total = 0
    for item in user['cart']:
        product = product_collection.find_one({'_id': ObjectId(item['product_id'])})
        if product:
            total += product['price'] * item['quantity']
        # Get supplier info from suppliers_collection using email from the product
        supplier_email = product.get('seller')
        supplier = sellers_collection.find_one({'email': supplier_email})
        supplier_name = supplier['name'] 
    print(supplier_name)
    tax = round(total * 0.18, 2)
    platform_fee = round(total * 0.02, 2)
    final_total = round(total + tax + platform_fee, 2)


    # Save payment in the payments collection
    payment_details = {
        'email': email,
        'card_name': card_name,
        'card_number': card_number,
        'card_cvv': card_cvv,
        'card_exp': card_exp,
        'delivery': delivery_address,
        'products': products,
        'total': total,
        'tax': tax,
        'supplier_name':supplier_name,
        'platform_fee': platform_fee,
        'final_amount': final_total,
        'status': 'Paid',
        'timestamp': datetime.datetime.now()
    }
    payment_result = payments_collection.insert_one(payment_details)

    # Get the inserted payment ID
    payment_id = payment_result.inserted_id

    # Process orders and update stock
    for item in products:
        # Get product details from product_collecti

        # Save order in the orders collection with payment_id
        order_details = {
            'email': email,
            'product_id': item['product_id'],
            'quantity': item['quantity'],
            'status': 'Paid',
            'final_amount':final_total,
            'supplier': supplier_name,
            'payment_id': payment_id,  # Add payment_id to the order
            'delivery': delivery_address,
            'timestamp': datetime.datetime.now()
        }
        orders_collection.insert_one(order_details)

        # Update the quantity of the product in the product collection
        new_quantity = product['quantity'] - item['quantity']
        product_collection.update_one({'_id': ObjectId(item['product_id'])}, {'$set': {'quantity': new_quantity}})

    # Empty the cart after successful payment
    customers_collection.update_one({'email': email}, {'$set': {'cart': []}})

    return redirect(url_for('buyer_dashboard'))


@app.route('/customer_view_payments')
def customer_view_payments():
    mail = session.get('email')
    if not mail:
        return redirect(url_for('login'))

    all_payments = payments_collection.find({'email': mail}).sort('timestamp', -1)  # Most recent first

    return render_template('customer_view_payments.html', payments=all_payments)


@app.route('/seller_view_payments')
def seller_view_payments():
    name = session.get('name')
    role = session.get('role')  # Get the role from the session
    
    if not name or not role:
        return redirect(url_for('login'))

    # If the user is an admin, fetch all payments
    if role == 'admin':
        all_payments = payments_collection.find().sort('timestamp', -1)  # Most recent first
    elif role == 'seller':
        # If the user is a seller, fetch payments related to their supplier name
        all_payments = payments_collection.find({'supplier_name': name}).sort('timestamp', -1)
    else:
        return redirect(url_for('login'))

    return render_template('seller_view_payments.html', payments=all_payments)

@app.route('/customer_orders')
def customer_orders():
    email = session.get('email')
    if not email:
        return redirect(url_for('login'))

    # Get all orders for the logged-in user
    orders_cursor = orders_collection.find({'email': email})
    orders = []

    for order in orders_cursor:
        product = product_collection.find_one({'_id': ObjectId(order['product_id'])})

        orders.append({
            'product_name': product['name'] if product else 'Unknown Product',
            'image': product['image'] if product and 'image' in product else '/static/images/default.jpg',
            'quantity': order.get('quantity', 0),
            'final_amount': order.get('final_amount', 0),
            'status': order.get('status', 'Pending'),
            'supplier': order.get('supplier', 'Unknown'),
            'payment_id': str(order.get('payment_id', '')),
            'timestamp': order.get('timestamp', datetime.datetime.now())
        })

    return render_template('customer_orders.html', orders=orders)




@app.route('/supplier_orders', methods=['GET', 'POST'])
def supplier_orders():
    # Get the supplier's name from the session
    supplier_name = session.get('name')
    role = session.get('role')
    
    if not supplier_name:
        return redirect(url_for('login')) 
    
     # If supplier is not logged in, redirect to login page

    # Query to get all orders where the supplier is the logged-in supplier
    orders = list(orders_collection.find({'supplier': supplier_name}))
    
    # Handle the form submission to update the order status
    if request.method == 'POST':
        order_id = request.form.get('order_id')
        new_status = request.form.get('status')

        # Update the status of the order
        orders_collection.update_one(
            {'_id': ObjectId(order_id)},
            {'$set': {'status': new_status}}
        )

        return redirect(url_for('supplier_orders'))  # Redirect to reload the page with updated status



    # Process each order to fetch customer name and product name
    for order in orders:
        # Get the customer email from the order
        customer_email = order['email']
        
        # Get customer name from the customers collection
        customer = customers_collection.find_one({'email': customer_email})
        order['customer_name'] = customer['name'] if customer else 'Unknown Customer'

        # Get product name from the products collection
        product = product_collection.find_one({'_id': ObjectId(order['product_id'])})
        order['product_name'] = product['name'] if product else 'Unknown Product'
    
    return render_template('supplier_orders.html', orders=orders, supplier_name=supplier_name)



@app.route('/admin_supplier_orders', methods=['GET', 'POST'])
def admin_supplier_orders():

     # If supplier is not logged in, redirect to login page

    # Query to get all orders where the supplier is the logged-in supplier
    orders = list(orders_collection.find())

    if request.method == 'POST':
        order_id = request.form.get('order_id')
        new_status = request.form.get('status')

        # Update the status of the order
        orders_collection.update_one(
            {'_id': ObjectId(order_id)},
            {'$set': {'status': new_status}}
        )

        return redirect(url_for('admin_supplier_orders'))  # Redirect to reload the page with updated status



    print(orders)
    for order in orders:
        # Get the customer email from the order
        customer_email = order['email']
        
        # Get customer name from the customers collection
        customer = customers_collection.find_one({'email': customer_email})
        order['customer_name'] = customer['name'] if customer else 'Unknown Customer'

        # Get product name from the products collection
        product = product_collection.find_one({'_id': ObjectId(order['product_id'])})
        order['product_name'] = product['name'] if product else 'Unknown Product'
    
    return render_template('admin_supplier_orders.html', orders=orders)


@app.route('/sup_browse_products', methods=['GET'])
def sup_browse_products():
    # Get the role and email from session
    role = session.get('role')
    email = session.get('email')

    # If the user is not logged in, redirect to login page
    if not role or not email:
        return redirect(url_for('login'))

    # Determine the products to fetch based on the role
    if role == 'admin':
        # Admin can see all products
        products = list(product_collection.find())
    elif role == 'seller':
        # Seller can only see their products
        products = list(product_collection.find({'seller': email}))
    else:
        # If role is unknown, redirect to login
        return redirect(url_for('login'))

    # Render the products on the supplier browse page
    return render_template('sup_browse.html', products=products)


@app.route('/edit_product/<product_id>', methods=['GET', 'POST'])
def edit_product(product_id):
    if 'email' not in session:
        return redirect(url_for('login'))

    # Convert to ObjectId
    try:
        product_obj_id = ObjectId(product_id)
    except:
        return "Invalid Product ID", 400

    # Fetch product
    product = product_collection.find_one({"_id": product_obj_id})
    if not product:
        return "Product not found!", 404

    # Fetch variant
    variant = variant_collection.find_one({"_id": product['variant_id']})
    if not variant:
        return "Variant not found!", 404

    if request.method == 'POST':
        # Get updated data from form
        updated_data = {
            "name": request.form['product_name'],
            "price": float(request.form['price']),
            "quantity": int(request.form['quantity']),
            "category_id": ObjectId(request.form['category']),
        }

        updated_variant = {
            "color": request.form['color'],
            "material": request.form['material'],
            "dimension": request.form['dimension'],
            "brand": request.form['brand'],
        }

        # Update both product and variant
        product_collection.update_one({"_id": product_obj_id}, {"$set": updated_data})
        variant_collection.update_one({"_id": product['variant_id']}, {"$set": updated_variant})

        if session['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('seller_dashboard'))

    # GET request - render form with current data
    categories = category_collection.find()
    return render_template('edit_product.html', product=product, variant=variant, categories=categories)

@app.route('/delete_product/<product_id>', methods=['POST'])
def delete_product(product_id):
    try:
        product_obj_id = ObjectId(product_id)
    except:
        return "Invalid product ID!", 400

    product = product_collection.find_one({'_id': product_obj_id})
    if not product:
        return "Product not found!", 404

    # Delete product image if exists
    if product.get('image') and os.path.exists(product['image']):
        os.remove(product['image'])

    # Delete the product
    product_collection.delete_one({'_id': product_obj_id})

    # Delete the associated variant
    if 'variant_id' in product:
        variant_collection.delete_one({'_id': product['variant_id']})


    if session['role'] == 'admin':
        return redirect(url_for('admin_dashboard'))
    else:
        return redirect(url_for('seller_dashboard'))
    
@app.route('/create_category', methods=['GET', 'POST'])
def create_category():
    if 'email' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        type_ = request.form['type']
        description = request.form['description']
        placement = request.form['placement']

        category_data = {
            "type": type_,
            "description": description,
            "placement": placement
        }

        category_collection.insert_one(category_data)
        return redirect(url_for('admin_dashboard'))  # or wherever you want to redirect

    return render_template('create_category.html')


if __name__ == '__main__':
    app.run(debug=True)