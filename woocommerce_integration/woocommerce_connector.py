from frappe.utils.data import cint
from woocommerce import API as WCAPI


class WooCommerceConnector:
    def __init__(self, setup: dict):
        self.settings = setup
        self.url = self.settings.url
        self.consumer_key = self.settings.consumer_key
        self.consumer_secret = self.settings.get_password("consumer_secret")
        self.woocommerce = WCAPI(
            url=self.url,
            consumer_key=self.consumer_key,
            consumer_secret=self.consumer_secret,
            wp_api=True,
            verify_ssl=bool(cint(self.settings.verify_ssl)),
            version="wc/v3",
            timeout=1000,
        )

    def _request(self, method, endpoint, data=None, params=None, **kwargs):
        woocomm_method = method.lower()
        positional_args = (
            (
                endpoint,
                data,
            )
            if woocomm_method in ("post", "put")
            else (endpoint,)
        )

        kwargs["params"] = params
        response = self.woocommerce.__getattribute__(woocomm_method)(
            *positional_args, **kwargs
        )
        response.raise_for_status()
        return response

    def get_products(self, **kwargs):
        response = self._request("GET", "products", params=kwargs)
        return response.json()

    def get_product(self, id: str):
        response = self._request("GET", f"products/{id}")
        return response.json()

    def create_product(self, product_data: dict):
        response = self._request("POST", "products", data=product_data)
        return response.json()

    def update_product(self, id: str, product_data: dict):
        response = self._request("PUT", f"products/{id}", data=product_data)
        return response.json()

    def batch_update_products(self, product_data: dict):
        response = self._request("POST", "products/batch", data=product_data)
        return response.json()

    def delete_product(self, id: str):
        response = self._request("DELETE", f"products/{id}")
        return response.json()

    def get_orders(self, **kwargs):
        response = self._request("GET", "orders", params=kwargs)
        yield from response.json()

        pages = cint(response.headers.get("X-WP-TotalPages") or 1)
        for page_idx in range(1, pages):
            response = self._request(
                "GET", "orders", params={**kwargs, "page": page_idx + 1}
            )
            yield from response.json()

    def get_order(self, id: str):
        return self._request("GET", f"orders/{id}").json()
