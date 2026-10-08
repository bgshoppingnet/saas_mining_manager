def install(module):
    def index_query(name):
        return f'''query {name} {{
  products {{
    edges {{
      node {{
        __typename
        id
        title
        handle
        vendor
        productType
        tags
        metafields(first: 10, namespace: "supplier_sync") {{
          edges {{
            node {{
              __typename
              id
              namespace
              key
              value
              type
            }}
          }}
        }}
        variants {{
          edges {{
            node {{
              __typename
              id
              sku
              barcode
              price
              inventoryItem {{ id tracked }}
              metafields(first: 10, namespace: "supplier_sync") {{
                edges {{
                  node {{
                    __typename
                    id
                    namespace
                    key
                    value
                    type
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}'''
    module.index_query = index_query
    return module
