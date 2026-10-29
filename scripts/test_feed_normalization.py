import unittest
from rebuild_channel_feeds import clean_collection_years,image_url,specific_category
class ProductBoundaryTests(unittest.TestCase):
 def test_collection_year_and_significant_numbers(self):
  self.assertEqual(clean_collection_years('Столче i-Size 40–150 см 2024 KikkaBoo'),'Столче i-Size 40–150 см KikkaBoo')
  for title in ['Протектор Nokia 6 (2018)','METABO TA E 2019','Телефон No brand 125-2024','Грил Tefal 2000 W','Играчка 3–6 години KikkaBoo']:
   self.assertEqual(clean_collection_years(title),title)
 def test_accessories_do_not_become_the_main_product(self):
  cases=[('Подложка За Количка GREY','Подложки за колички'),('Раница За Количка TINA','Чанти и раници за колички'),("Външна Гума 12 за триколка",'Резервни гуми за триколки'),('Играчка за количка спирала с гризалки','Играчки за количка')]
  for name,leaf in cases:
   if name.startswith('Играчка'):name='Спирала за количка с гризалки'
   self.assertTrue(specific_category({'name':name,'category':'Аксесоари'}).endswith(leaf),name)
 def test_toys_do_not_become_power_tools(self):
  cases=[('Детски комплект инструменти с каска 17 части','Детски комплекти инструменти'),('Детски крикет - Животни','Детски комплекти за крикет')]
  for name,leaf in cases:self.assertTrue(specific_category({'name':name,'category':'Играчки'}).endswith(leaf))
 def test_textile_decoration_is_not_a_toy(self):
  self.assertEqual(specific_category({'name':'Сет 4 части Ранфорс Влакче Сиво','category':'Текстил'}),'Бебешки текстил > Спални комплекти')
 def test_image_url_encoding(self):
  self.assertEqual(image_url('https://example.com/детска снимка.jpg?v=1&amp;width=600'),'https://example.com/%D0%B4%D0%B5%D1%82%D1%81%D0%BA%D0%B0%20%D1%81%D0%BD%D0%B8%D0%BC%D0%BA%D0%B0.jpg?v=1&width=600')
  for value in ['','javascript:bad','https://example.com/placeholder.jpg','https://user:password@example.com/a.jpg']:
   self.assertEqual(image_url(value),'')
if __name__=='__main__':unittest.main()
