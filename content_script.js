let timeoutTime = 5000;

//The key (left side, i.E. '3070' ) of this priceobject must be contained inside the title of the item. It is recommended to copy as much as possible (if not the entire title) in order to avoid unwanted outcomes
//Further explaination: item: nvidia 3070 -> maxprice: 650
let priceObject = {
  "3070": 650,
  "3080": 850,
};

console.log("Extension loaded successfully!");

recursiveBuyButtonLookup();

function recursiveBuyButtonLookup() {
  let toCartButton = document.getElementById("add-to-cart-button");
  let buyButton = document.getElementById("buy-now-button");

  if (buyButton != null) {
    console.log("Able to buy!!");
    let itemTitle = document.getElementById("productTitle").innerText;
    let itemPrice = document.getElementById("priceblock_ourprice").innerText;
    itemPrice = itemPrice.replace(",", ".");
    itemPrice = itemPrice.replace("€", "");
    itemPrice = itemPrice.replace("$", "");

    for (let key in priceObject) {
      if (itemTitle.indexOf(key) >= 0) {
        if (itemPrice <= priceObject[key]) {
          buyButton.click();
          alert("Attempting to buy...");
          break;
        } else {
          console.log(
            "Item is too expensive at " +
              itemPrice +
              " my max Price is: " +
              priceObject[key]
          );
        }
      }
    }
  } else {
    console.log("no button found :(");
    setTimeout(function () {
      location.reload();
    }, timeoutTime);
  }
}
