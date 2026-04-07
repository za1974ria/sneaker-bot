"""API routes."""

from flask import Blueprint, jsonify

from app.services.market_service import get_market_products

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/products")
def get_products():
    products = get_market_products()
    return jsonify(products)
