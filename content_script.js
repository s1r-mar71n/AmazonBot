let timeoutTime = 5000;

//The key (left side, i.E. '3070' ) of this priceobject must be contained inside the title of the item. It is recommended to copy as much as possible (if not the entire title) in order to avoid unwanted outcomes
//Further explaination: item: nvidia 3070 -> maxprice: 650
let priceObject = {
  "3070": 650,
  "3080": 850,
  "Ryzen 7 5800X": 480, 
  "Ryzen 9 5900X": 600
};

//recursiveBuyButtonLookup();
checkPage(); 

function checkPage(){
  //Redirect Page after 'add to cart'
  if(document.URL.indexOf('https://www.amazon.de/gp/huc')>=0 || document.URL.indexOf('https://www.amazon.de/gp/yourstore')>=0){
    window.open('https://www.amazon.de/gp/buy/spc/handlers/display.html?hasWorkingJavascript=1','_self');
  }else if(document.URL == 'https://www.amazon.de/gp/buy/spc/handlers/display.html?hasWorkingJavascript=1'){
    validateCartAndBuy(); 
  }else {
    recursiveBuyButtonLookup(); 
  }
}

function validateCartAndBuy(){
  let nameFromLocalStorage = localStorage.getItem('itemName'); 

  //See if it is contained in HTML
  let entireHTMLAsString = document.getElementsByTagName('html')[0].innerText; 

  if(entireHTMLAsString.indexOf(nameFromLocalStorage)>=0){
    let maxPrice = Number(localStorage.getItem('itemPrice')); 

    let totalCheckoutPrice = document.getElementsByClassName('a-color-price a-size-medium a-text-right grand-total-price aok-nowrap a-text-bold a-nowrap')[0].innerText; 
    totalCheckoutPrice = cleanUpItemPrice(totalCheckoutPrice); 
    if(totalCheckoutPrice <= maxPrice){
      let buyNowButton = document.getElementsByName('placeYourOrder1')[0];
      buyNowButton.click(); 
    }else{
      localStorage.setItem('itemName', null); 
      localStorage.setItem('itemPrice', null); 
      window.alert('price must have changed in cart...');
    }

  }else{
    localStorage.setItem('itemName', null); 
    localStorage.setItem('itemPrice', null);
    window.alert('Something went wrong, names did not match'); 
  }

}

function recursiveBuyButtonLookup() {
  requestNotificationPermissionIfDefault(); 
  let addToCartButton = document.getElementById("add-to-cart-button");
  
  if (addToCartButton != null) {
    console.log("Able to buy!!");
    let itemTitle = document.getElementById("productTitle").innerText;
    let itemPrice = document.getElementById("priceblock_ourprice").innerText;
    itemPrice = cleanUpItemPrice(itemPrice); 

    for (let key in priceObject) {
      if (itemTitle.indexOf(key) >= 0) {
        if (itemPrice <= priceObject[key]) {
          sendNotification(`Attempting to buy ${itemTitle} at ${itemPrice} / ${priceObject[key]}`);
          localStorage.setItem('itemName', itemTitle); 
          localStorage.setItem('itemPrice', priceObject[key]); 
          addToCartButton.click();
          break;
        } else {
          //sendNotification(itemTitle +" is too expensive at " + itemPrice + " my max Price is: " + priceObject[key]);
          reloadCurrentPageAfterSpecifiedTimeout(); 
        }
      }
    }
  } else {
    console.log("no button found :(");
    reloadCurrentPageAfterSpecifiedTimeout(); 
  }
}

function requestNotificationPermissionIfDefault(){
  if(Notification.permission == 'default'){
    Notification.requestPermission(); 
  }
}

function sendNotification(notificationMessage){
  new Notification(notificationMessage).onclick = function () {
    window.focus();
  }; 
}

function cleanUpItemPrice(itemPrice){
  itemPrice = itemPrice.replace(",", ".");
  itemPrice = itemPrice.replace("€", "");
  itemPrice = itemPrice.replace("$", "");
  itemPrice = itemPrice.replace("EUR ", ""); 
  return itemPrice; 
}

function reloadCurrentPageAfterSpecifiedTimeout(){
  setTimeout(function () {
    location.reload();
  }, timeoutTime);
}