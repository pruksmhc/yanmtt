from transformers import AutoConfig, AlbertTokenizer, AutoTokenizer, MBartTokenizer
import sys
import os

if sys.argv[2] == "albert":
    tokenizer = AlbertTokenizer.from_pretrained(sys.argv[1], do_lower_case=False, use_fast=False, keep_accents=True, strip_accents=False)
elif sys.argv[2] == "mbart":
    tokenizer = MBartTokenizer.from_pretrained(sys.argv[1], do_lower_case=False, use_fast=False, keep_accents=True, strip_accents=False)
else:
    print("Unknown tokenizer. Exiting!")
    sys.exit(1)

tokenizer.save_pretrained(sys.argv[1])

os.rename(sys.argv[1]+"/tokenizer_config.json",sys.argv[1]+"/config.json")

config = AutoConfig.from_pretrained(sys.argv[1])
config.save_pretrained(sys.argv[1])

print("Testing tokenizer")

tokenizer = AutoTokenizer.from_pretrained(sys.argv[1], do_lower_case=False, use_fast=False, keep_accents=True, strip_accents=False)

if sys.argv[2] == "albert":
    print(tokenizer.convert_ids_to_tokens(tokenizer("I am Gone. Làm sao tôi có thể trình bày trong 10 phút về sợi dây liên kết những người phụ nữ qua ba thế hệ , về việc làm thế nào những sợi dây mạnh mẽ đáng kinh ngạc ấy đã níu chặt lấy cuộc sống của một cô bé bốn tuổi co quắp với đứa em gái nhỏ của cô bé ,", add_special_tokens=False).input_ids))
elif sys.argv[2] == "mbart":
    print(tokenizer.convert_ids_to_tokens(tokenizer("I am Gone. Làm sao tôi có thể trình bày trong 10 phút về sợi dây liên kết những người phụ nữ qua ba thế hệ , về việc làm thế nào những sợi dây mạnh mẽ đáng kinh ngạc ấy đã níu chặt lấy cuộc sống của một cô bé bốn tuổi co quắp với đứa em gái nhỏ của cô bé ,", add_special_tokens=False).input_ids))
    
print("Success")
